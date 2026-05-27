"""Coleta datasets PT-BR para fine-tuning LoRA do mtzcode.

Baixa subsets de datasets HuggingFace em ``~/.mtzcode/finetune/raw/`` e também
extrai pares user→assistant dos logs locais de sessão (``~/.mtzcode/logs/``).

Uso::

    python -m mtzcode.finetune.collect_data
    python -m mtzcode.finetune.collect_data --max-samples 5000
    python -m mtzcode.finetune.collect_data --skip wiki oscar
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

# Diretórios padrão usados pelo pipeline inteiro.
RAW_DIR = Path.home() / ".mtzcode" / "finetune" / "raw"
LOGS_DIR = Path.home() / ".mtzcode" / "logs"


# Catálogo de datasets que sabemos baixar. Cada entrada vira um arquivo
# ``raw/{nome}.jsonl`` no final.
DATASETS: dict[str, dict[str, Any]] = {
    "wiki_pt": {
        "hf_id": "wikipedia",
        "config": "20220301.pt",
        "split": "train",
        "text_fields": ["text"],
        "title_field": "title",
    },
    "oscar_pt": {
        "hf_id": "oscar-corpus/OSCAR-2301",
        "config": "pt",
        "split": "train",
        "text_fields": ["text"],
    },
    "pira_qa": {
        "hf_id": "paulopirozelli/pira",
        "config": None,
        "split": "train",
        # Pirá tem perguntas/respostas bilíngues; usamos apenas as PT-BR.
        "qa_fields": ("question_pt_origin", "answer_pt_origin"),
    },
}


def _ensure_dirs() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)


def _lazy_import_datasets():
    """Importa ``datasets`` de forma preguiçosa, com mensagem clara."""
    try:
        import datasets  # type: ignore
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        print(
            "[erro] biblioteca 'datasets' não instalada.\n"
            "       instale com: pip install datasets",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    return datasets


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    """Escreve um iterável de dicts como JSONL e retorna a contagem."""
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def _download_one(name: str, spec: dict[str, Any], max_samples: int) -> Path:
    """Baixa um dataset específico e materializa em ``raw/{name}.jsonl``."""
    datasets = _lazy_import_datasets()
    print(f"[coleta] baixando {name} ({spec['hf_id']})...")

    load_kwargs: dict[str, Any] = {"split": spec["split"], "streaming": True}
    if spec.get("config"):
        load_kwargs["name"] = spec["config"]

    try:
        ds = datasets.load_dataset(spec["hf_id"], **load_kwargs)
    except Exception as exc:  # pragma: no cover - depende de rede
        print(f"[aviso] falhou {name}: {exc}", file=sys.stderr)
        return RAW_DIR / f"{name}.jsonl"

    out_path = RAW_DIR / f"{name}.jsonl"

    def _iter_rows():
        for i, sample in enumerate(ds):
            if i >= max_samples:
                break
            # Datasets QA têm formato dedicado.
            if "qa_fields" in spec:
                q_field, a_field = spec["qa_fields"]
                question = (sample.get(q_field) or "").strip()
                answer = (sample.get(a_field) or "").strip()
                if question and answer:
                    yield {"source": name, "question": question, "answer": answer}
                continue
            # Datasets de texto puro (wiki, OSCAR).
            text = " ".join(
                str(sample.get(f, "")) for f in spec.get("text_fields", [])
            ).strip()
            if not text:
                continue
            row: dict[str, Any] = {"source": name, "text": text}
            if "title_field" in spec and spec["title_field"] in sample:
                row["title"] = sample[spec["title_field"]]
            yield row

    written = _write_jsonl(out_path, _iter_rows())
    print(f"[coleta] {name}: {written} amostras → {out_path}")
    return out_path


def collect_own_logs(logs_dir: Path = LOGS_DIR) -> Path:
    """Extrai pares user→assistant dos logs locais do mtzcode.

    Procura arquivos ``*.jsonl`` em ``logs_dir`` (gerados pelo ``session_log``)
    e gera ``raw/own_logs.jsonl`` com pares conversacionais limpos.
    """
    out_path = RAW_DIR / "own_logs.jsonl"
    if not logs_dir.exists():
        print(f"[logs] diretório {logs_dir} não existe, pulando.")
        _write_jsonl(out_path, [])
        return out_path

    print(f"[logs] varrendo {logs_dir} ...")
    pairs: list[dict[str, Any]] = []
    last_user: str | None = None

    for log_file in sorted(logs_dir.glob("*.jsonl")):
        try:
            with log_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role = event.get("role") or event.get("type")
                    content = event.get("content") or event.get("text") or ""
                    if not isinstance(content, str):
                        continue
                    content = content.strip()
                    if not content:
                        continue
                    if role in ("user", "human"):
                        last_user = content
                    elif role in ("assistant", "gpt", "ai") and last_user:
                        pairs.append(
                            {
                                "source": "own_logs",
                                "question": last_user,
                                "answer": content,
                                "log_file": log_file.name,
                            }
                        )
                        last_user = None
        except OSError as exc:  # pragma: no cover
            print(f"[logs] erro lendo {log_file}: {exc}", file=sys.stderr)

    written = _write_jsonl(out_path, pairs)
    print(f"[logs] {written} pares extraídos → {out_path}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Coleta datasets PT-BR para fine-tuning LoRA."
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=10_000,
        help="máximo de amostras por dataset HF (default: 10000).",
    )
    parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        choices=list(DATASETS.keys()) + ["own_logs"],
        help="datasets a pular.",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        choices=list(DATASETS.keys()) + ["own_logs"],
        help="se passado, baixa apenas estes.",
    )
    args = parser.parse_args()

    _ensure_dirs()
    print(f"[coleta] saída: {RAW_DIR}")

    selected = args.only if args.only else list(DATASETS.keys()) + ["own_logs"]

    for name in selected:
        if name in args.skip:
            print(f"[coleta] pulando {name}")
            continue
        if name == "own_logs":
            collect_own_logs()
        else:
            _download_one(name, DATASETS[name], args.max_samples)

    print("[coleta] concluído.")


if __name__ == "__main__":
    main()
