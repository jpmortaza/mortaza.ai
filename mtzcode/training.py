"""Modo de treinamento (fine-tuning) — gerencia datasets e jobs LoRA.

Camada de orquestração entre a web UI e o pipeline ``mtzcode.finetune``.
Mantém um único job ativo por processo (training é caro, não faz sentido
paralelizar). Persiste logs em arquivo pra UI conseguir tail.

Layout em disco:
    ~/.mtzcode/finetune/raw/        — datasets brutos enviados pelo usuário
    ~/.mtzcode/finetune/formatted/  — train.jsonl/valid.jsonl gerados
    ~/.mtzcode/finetune/adapters/   — adapters LoRA treinados
    ~/.mtzcode/finetune/logs/       — stdout/stderr de cada run
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

FINETUNE_DIR = Path.home() / ".mtzcode" / "finetune"
RAW_DIR = FINETUNE_DIR / "raw"
FORMATTED_DIR = FINETUNE_DIR / "formatted"
ADAPTER_DIR = FINETUNE_DIR / "adapters"
LOGS_DIR = FINETUNE_DIR / "logs"

ALLOWED_EXTS = {".jsonl", ".json", ".txt", ".md"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB


def _ensure_dirs() -> None:
    for d in (RAW_DIR, FORMATTED_DIR, ADAPTER_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------
# Python interpreter discovery — mlx-lm pode estar em outro env
# ----------------------------------------------------------------------
def _candidate_pythons() -> list[str]:
    """Lista de interpretadores Python a testar (sem duplicatas).

    Ordem: override do usuário → sys.executable → conda envs comuns →
    pythons no PATH. O primeiro que tiver mlx_lm instalado vence.
    """
    home = Path.home()
    cands: list[str] = []

    # 1) Override explícito (env var ou settings)
    override = os.environ.get("MTZCODE_TRAINING_PYTHON")
    if override:
        cands.append(override)
    try:
        from mtzcode.settings import get_settings
        opt = (get_settings().training_python or "").strip()
        if opt:
            cands.append(opt)
    except Exception:
        pass

    # 2) Interpretador atual
    cands.append(sys.executable)

    # 3) Envs conda/venv comuns no Mac do Jean
    for rel in (
        "radioconda/bin/python",
        "miniconda3/bin/python",
        "anaconda3/bin/python",
        "miniforge3/bin/python",
        ".venv/bin/python",
    ):
        cands.append(str(home / rel))

    # 4) PATH
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            cands.append(found)

    # Dedupe preservando ordem
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        if c and c not in seen and Path(c).exists():
            seen.add(c)
            out.append(c)
    return out


def _python_has_mlx(python_path: str) -> bool:
    """Testa se um interpretador específico tem mlx_lm importável."""
    try:
        r = subprocess.run(
            [python_path, "-c", "import mlx_lm"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_mlx_python() -> str | None:
    """Retorna o primeiro Python encontrado que tem mlx_lm instalado."""
    for py in _candidate_pythons():
        if _python_has_mlx(py):
            return py
    return None


# ----------------------------------------------------------------------
# Datasets — upload, list, delete
# ----------------------------------------------------------------------
def list_datasets() -> list[dict[str, Any]]:
    """Lista arquivos em RAW_DIR com metadados básicos."""
    _ensure_dirs()
    out: list[dict[str, Any]] = []
    for p in sorted(RAW_DIR.iterdir(), key=lambda x: x.name):
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        # Conta linhas se for jsonl/json (rápido até ~50MB)
        line_count: int | None = None
        if p.suffix.lower() in (".jsonl", ".json") and stat.st_size < 50 * 1024 * 1024:
            try:
                with p.open("rb") as fh:
                    line_count = sum(1 for _ in fh)
            except OSError:
                line_count = None
        out.append(
            {
                "name": p.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "lines": line_count,
                "ext": p.suffix.lower(),
            }
        )
    return out


def save_dataset(filename: str, content: bytes) -> dict[str, Any]:
    """Grava um dataset enviado pela UI em RAW_DIR.

    Sanitiza o nome do arquivo e valida extensão/tamanho.
    """
    _ensure_dirs()
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"arquivo grande demais (>{MAX_UPLOAD_BYTES // (1024*1024)}MB)")
    safe_name = Path(filename).name  # remove qualquer ../
    if not safe_name or safe_name.startswith("."):
        raise ValueError("nome de arquivo inválido")
    ext = Path(safe_name).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(
            f"extensão {ext} não suportada. Use: {', '.join(sorted(ALLOWED_EXTS))}"
        )
    target = RAW_DIR / safe_name
    target.write_bytes(content)
    return {
        "name": safe_name,
        "size": len(content),
        "path": str(target),
    }


def format_datasets(val_ratio: float = 0.05, seed: int = 42) -> dict[str, Any]:
    """Roda o pipeline format_data — converte raw/*.jsonl em train/valid.jsonl.

    Usado pelo botão "Formatar agora" da UI quando o usuário tem datasets
    brutos (Pirá, wiki, OSCAR, próprios logs) mas ainda não rodou o
    formatter. Não exige subir um train.jsonl pronto.
    """
    _ensure_dirs()
    import random as _random
    from mtzcode.finetune import format_data as _fd

    raw_files = sorted(
        p for p in RAW_DIR.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.suffix.lower() in {".jsonl", ".json", ".txt", ".md"}
    )
    if not raw_files:
        raise RuntimeError(
            "nenhum arquivo em raw/. Suba algo em Datasets primeiro: "
            ".jsonl (ShareGPT/QA), .txt ou .md (texto puro — gera "
            "perguntas sintéticas automaticamente)."
        )

    rng = _random.Random(seed)
    examples: list[dict[str, Any]] = []
    per_file: dict[str, int] = {}

    def _chunks_from_text(content: str, chunk_size: int = 1500) -> list[tuple[str, str]]:
        """Quebra texto em pedaços. Tenta usar parágrafos; se muito grandes, fatia."""
        out: list[tuple[str, str]] = []
        # Tenta separar por linhas em branco (parágrafos)
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        buf = ""
        for p in paragraphs:
            if len(buf) + len(p) + 2 <= chunk_size:
                buf = (buf + "\n\n" + p).strip()
            else:
                if buf:
                    out.append(("", buf))
                if len(p) <= chunk_size:
                    buf = p
                else:
                    # parágrafo gigante — fatia em janelas
                    for i in range(0, len(p), chunk_size):
                        out.append(("", p[i:i + chunk_size]))
                    buf = ""
        if buf:
            out.append(("", buf))
        return out

    for raw_file in raw_files:
        count = 0
        ext = raw_file.suffix.lower()
        if ext in (".jsonl", ".json"):
            for row in _fd._iter_raw_file(raw_file):
                ex: dict[str, Any] | None
                if "conversations" in row:
                    ex = row
                elif "question" in row and "answer" in row:
                    ex = _fd._format_qa(row)
                elif "text" in row:
                    ex = _fd._format_text(row, rng)
                else:
                    ex = None
                if ex is not None:
                    examples.append(ex)
                    count += 1
        else:
            # .txt / .md — vira texto puro com sintético
            try:
                content = raw_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            # Usa o nome do arquivo como "título" pra prompts melhores
            title = raw_file.stem.replace("-", " ").replace("_", " ").strip()
            for _, chunk in _chunks_from_text(content):
                if len(chunk) < 80:
                    continue
                row = {"text": chunk, "title": title}
                ex = _fd._format_text(row, rng)
                if ex is not None:
                    examples.append(ex)
                    count += 1
        per_file[raw_file.name] = count

    if not examples:
        raise RuntimeError(
            "nenhum exemplo válido foi gerado. Verifique se os arquivos têm "
            "o formato esperado (ShareGPT, QA ou texto puro)."
        )

    rng.shuffle(examples)
    train, valid = _fd.split_train_valid(examples, val_ratio)
    train_path = FORMATTED_DIR / "train.jsonl"
    valid_path = FORMATTED_DIR / "valid.jsonl"
    n_train = _fd._write_jsonl(train_path, train)
    n_valid = _fd._write_jsonl(valid_path, valid)
    return {
        "ok": True,
        "train": n_train,
        "valid": n_valid,
        "per_file": per_file,
        "train_path": str(train_path),
        "valid_path": str(valid_path),
    }


def delete_dataset(filename: str) -> bool:
    """Apaga um dataset por nome (sanitizado)."""
    _ensure_dirs()
    safe_name = Path(filename).name
    target = RAW_DIR / safe_name
    if not target.exists() or not target.is_file():
        return False
    target.unlink()
    return True


# ----------------------------------------------------------------------
# Adapters — modelos treinados
# ----------------------------------------------------------------------
def list_adapters() -> list[dict[str, Any]]:
    """Lista adapters LoRA já treinados."""
    _ensure_dirs()
    out: list[dict[str, Any]] = []
    if not ADAPTER_DIR.exists():
        return out
    for p in sorted(ADAPTER_DIR.iterdir(), key=lambda x: x.name):
        if not p.is_dir():
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        out.append(
            {
                "name": p.name,
                "path": str(p),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            }
        )
    return out


# ----------------------------------------------------------------------
# Job manager — single training process at a time
# ----------------------------------------------------------------------
class TrainingJob:
    def __init__(self) -> None:
        self.id: str = ""
        self.status: str = "idle"  # idle | running | done | error | cancelled
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.cmd: list[str] = []
        self.log_path: Path | None = None
        self.return_code: int | None = None
        self.error: str | None = None
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cmd": " ".join(self.cmd) if self.cmd else None,
            "log_path": str(self.log_path) if self.log_path else None,
            "return_code": self.return_code,
            "error": self.error,
        }

    def is_running(self) -> bool:
        return self.status == "running" and self._proc is not None and self._proc.poll() is None


_job = TrainingJob()


def get_job() -> TrainingJob:
    return _job


def check_mlx_lm() -> dict[str, Any]:
    """Retorna info se mlx-lm está disponível em ALGUM Python acessível.

    Não exige que esteja no mesmo venv do servidor — testa vários
    interpretadores (sys.executable, conda envs, PATH) e usa o primeiro
    que conseguir importar ``mlx_lm``.
    """
    py = find_mlx_python()
    if py:
        return {"installed": True, "python": py}
    tried = _candidate_pythons()
    return {
        "installed": False,
        "error": "mlx_lm não encontrado em nenhum interpretador",
        "tried": tried,
    }


def start_training(
    model: str = "Qwen/Qwen2.5-14B-Instruct",
    iters: int = 500,
    batch_size: int = 2,
    lora_layers: int = 16,
    learning_rate: float = 1e-5,
) -> dict[str, Any]:
    """Dispara mlx_lm.lora num subprocess. Bloqueia se já houver job rodando."""
    _ensure_dirs()
    with _job._lock:
        if _job.is_running():
            raise RuntimeError("já existe um treinamento em execução")

        # Pré-checks — usa o python que tem mlx_lm
        mlx = check_mlx_lm()
        if not mlx.get("installed"):
            raise RuntimeError(
                "mlx-lm não encontrado em nenhum Python acessível. "
                "Rode `pip install mlx-lm` no env de sua preferência ou "
                "configure MTZCODE_TRAINING_PYTHON / Configurações > "
                "training_python."
            )
        python_exe = mlx.get("python") or sys.executable
        # O usuário pode subir train.jsonl/valid.jsonl direto em RAW; copia
        # pra FORMATTED se ainda não existir lá. Isso atende o caso "subi
        # meu próprio dataset já formatado".
        train_src = RAW_DIR / "train.jsonl"
        valid_src = RAW_DIR / "valid.jsonl"
        if train_src.exists():
            shutil.copy(train_src, FORMATTED_DIR / "train.jsonl")
        if valid_src.exists():
            shutil.copy(valid_src, FORMATTED_DIR / "valid.jsonl")

        if not (FORMATTED_DIR / "train.jsonl").exists():
            raise RuntimeError(
                "nenhum train.jsonl encontrado. Suba um arquivo train.jsonl "
                "(formato ShareGPT) em Datasets, ou rode "
                "`python -m mtzcode.finetune.format_data` primeiro."
            )
        if not (FORMATTED_DIR / "valid.jsonl").exists():
            # mlx-lm exige valid.jsonl — duplica train se não tiver
            shutil.copy(
                FORMATTED_DIR / "train.jsonl", FORMATTED_DIR / "valid.jsonl"
            )

        job_id = uuid.uuid4().hex[:8]
        log_path = LOGS_DIR / f"train-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{job_id}.log"

        cmd = [
            python_exe,
            "-m",
            "mlx_lm.lora",
            "--model", model,
            "--train",
            "--data", str(FORMATTED_DIR),
            "--batch-size", str(batch_size),
            "--num-layers", str(lora_layers),
            "--iters", str(iters),
            "--learning-rate", str(learning_rate),
            "--adapter-path", str(ADAPTER_DIR),
        ]

        _job.id = job_id
        _job.status = "running"
        _job.started_at = datetime.now().isoformat(timespec="seconds")
        _job.finished_at = None
        _job.cmd = cmd
        _job.log_path = log_path
        _job.return_code = None
        _job.error = None

        # Abre log em append e roda o subprocess sem bloquear
        log_fh = log_path.open("w", encoding="utf-8")
        log_fh.write(f"# mtzcode training job {job_id}\n")
        log_fh.write(f"# started: {_job.started_at}\n")
        log_fh.write(f"# cmd: {' '.join(cmd)}\n\n")
        log_fh.flush()

        try:
            _job._proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=str(FINETUNE_DIR),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except OSError as exc:
            _job.status = "error"
            _job.error = str(exc)
            _job.finished_at = datetime.now().isoformat(timespec="seconds")
            log_fh.close()
            raise RuntimeError(f"falha ao iniciar processo: {exc}") from exc

        def _waiter() -> None:
            assert _job._proc is not None
            rc = _job._proc.wait()
            try:
                log_fh.close()
            except Exception:
                pass
            with _job._lock:
                _job.return_code = rc
                _job.finished_at = datetime.now().isoformat(timespec="seconds")
                if _job.status == "cancelled":
                    pass
                elif rc == 0:
                    _job.status = "done"
                else:
                    _job.status = "error"
                    _job.error = f"mlx_lm.lora saiu com código {rc}"

        _job._thread = threading.Thread(target=_waiter, daemon=True)
        _job._thread.start()
        return _job.to_dict()


def stop_training() -> dict[str, Any]:
    """Mata o processo de treinamento se estiver rodando."""
    with _job._lock:
        if not _job.is_running():
            return _job.to_dict()
        proc = _job._proc
        _job.status = "cancelled"
    try:
        if proc is not None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except OSError:
        pass
    return _job.to_dict()


def tail_log(max_lines: int = 200) -> str:
    """Retorna últimas N linhas do log do job atual."""
    if _job.log_path is None or not _job.log_path.exists():
        return ""
    try:
        with _job.log_path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    return "".join(lines[-max_lines:])


def status() -> dict[str, Any]:
    """Snapshot completo do estado atual de treinamento."""
    _ensure_dirs()
    mlx = check_mlx_lm()
    info = _job.to_dict()
    info["mlx_installed"] = mlx.get("installed", False)
    info["mlx_error"] = mlx.get("error")
    info["dirs"] = {
        "raw": str(RAW_DIR),
        "formatted": str(FORMATTED_DIR),
        "adapters": str(ADAPTER_DIR),
        "logs": str(LOGS_DIR),
    }
    info["dataset_count"] = len(list_datasets())
    info["adapter_count"] = len(list_adapters())
    return info
