"""Wrapper para treinar LoRA com ``mlx_lm.lora`` em PT-BR.

Constrói o comando ``python -m mlx_lm.lora ...`` com os parâmetros corretos
para o pipeline do mtzcode e dispara via ``subprocess``. Faz import preguiçoso
de ``mlx_lm`` apenas para validar instalação.

Uso típico::

    python -m mtzcode.finetune.train_lora
    python -m mtzcode.finetune.train_lora --model Qwen/Qwen2.5-7B-Instruct --iters 500
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path.home() / ".mtzcode" / "finetune" / "formatted"
ADAPTER_DIR = Path.home() / ".mtzcode" / "finetune" / "adapters"

DEFAULT_MODEL = "Qwen/Qwen2.5-14B-Instruct"


def _check_mlx_lm() -> None:
    """Garante que ``mlx_lm`` está disponível antes de tentar treinar."""
    try:
        import mlx_lm  # type: ignore  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        print(
            "[erro] 'mlx_lm' não está instalado.\n"
            "       instale com: pip install mlx-lm",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


def _check_data() -> None:
    train = DATA_DIR / "train.jsonl"
    valid = DATA_DIR / "valid.jsonl"
    if not train.exists() or not valid.exists():
        print(
            f"[erro] dados formatados não encontrados em {DATA_DIR}.\n"
            "       rode antes:\n"
            "         python -m mtzcode.finetune.collect_data\n"
            "         python -m mtzcode.finetune.format_data",
            file=sys.stderr,
        )
        raise SystemExit(1)


def build_command(args: argparse.Namespace) -> list[str]:
    """Monta a linha de comando que será passada para ``mlx_lm.lora``."""
    cmd: list[str] = [
        sys.executable,
        "-m",
        "mlx_lm.lora",
        "--model",
        args.model,
        "--train",
        "--data",
        str(args.data),
        "--batch-size",
        str(args.batch_size),
        "--lora-layers",
        str(args.lora_layers),
        "--iters",
        str(args.iters),
        "--adapter-path",
        str(args.adapter_path),
        "--learning-rate",
        str(args.learning_rate),
    ]
    if args.steps_per_eval:
        cmd += ["--steps-per-eval", str(args.steps_per_eval)]
    if args.save_every:
        cmd += ["--save-every", str(args.save_every)]
    if args.grad_checkpoint:
        cmd += ["--grad-checkpoint"]
    if args.resume_adapter_file:
        cmd += ["--resume-adapter-file", str(args.resume_adapter_file)]
    return cmd


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Treina LoRA PT-BR via mlx_lm.lora (Apple Silicon)."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="modelo base HF.")
    parser.add_argument(
        "--data",
        type=Path,
        default=DATA_DIR,
        help="diretório com train.jsonl/valid.jsonl.",
    )
    parser.add_argument(
        "--adapter-path",
        type=Path,
        default=ADAPTER_DIR,
        help="onde salvar os adapters LoRA.",
    )
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--lora-layers", type=int, default=16)
    parser.add_argument("--iters", type=int, default=1000)
    parser.add_argument("--learning-rate", type=float, default=1e-5)
    parser.add_argument("--steps-per-eval", type=int, default=100)
    parser.add_argument("--save-every", type=int, default=200)
    parser.add_argument(
        "--grad-checkpoint",
        action="store_true",
        help="ativa gradient checkpointing (economiza RAM).",
    )
    parser.add_argument(
        "--resume-adapter-file",
        type=Path,
        default=None,
        help="caminho de adapter existente para retomar treino.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="apenas imprime o comando, não executa.",
    )
    args = parser.parse_args()

    _check_mlx_lm()
    _check_data()

    args.adapter_path.mkdir(parents=True, exist_ok=True)

    cmd = build_command(args)
    pretty = " ".join(shutil.which(cmd[0]) and c or c for c in cmd)
    print("[treino] comando:")
    print("  " + pretty)

    if args.dry_run:
        print("[treino] dry-run, nada foi executado.")
        return

    print(f"[treino] modelo: {args.model}")
    print(f"[treino] dados:  {args.data}")
    print(f"[treino] saída:  {args.adapter_path}")
    print("[treino] iniciando... isso pode demorar horas.")

    try:
        result = subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        print("\n[treino] interrompido pelo usuário.")
        raise SystemExit(130)

    if result.returncode != 0:
        print(f"[treino] mlx_lm.lora falhou (exit={result.returncode}).", file=sys.stderr)
        raise SystemExit(result.returncode)

    print("[treino] concluído com sucesso.")
    print(f"[treino] adapters em: {args.adapter_path}")
    print("[treino] próximo passo: python -m mtzcode.finetune.export_ollama")


if __name__ == "__main__":
    main()
