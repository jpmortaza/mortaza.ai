"""Exporta o LoRA treinado em PT-BR para um modelo Ollama.

Pipeline:

1. ``mlx_lm.fuse`` — junta o adapter LoRA com o modelo base e salva uma versão
   "fundida" em HuggingFace format.
2. ``llama.cpp/convert_hf_to_gguf.py`` — converte o modelo fundido para GGUF
   (quantização opcional).
3. Gera um ``Modelfile`` Ollama (``FROM`` + ``SYSTEM`` em PT-BR + parâmetros).
4. Imprime o comando ``ollama create`` final.

Uso::

    python -m mtzcode.finetune.export_ollama \\
        --model Qwen/Qwen2.5-14B-Instruct \\
        --adapter-path ~/.mtzcode/finetune/adapters \\
        --llama-cpp ~/dev/llama.cpp
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

FINETUNE_DIR = Path.home() / ".mtzcode" / "finetune"
DEFAULT_ADAPTER = FINETUNE_DIR / "adapters"
DEFAULT_FUSED = FINETUNE_DIR / "fused"
DEFAULT_GGUF_DIR = FINETUNE_DIR / "gguf"
DEFAULT_MODELFILE = FINETUNE_DIR / "Modelfile"

DEFAULT_MODEL = "Qwen/Qwen2.5-14B-Instruct"

SYSTEM_PROMPT_PT = (
    "Você é o mtzcode, um assistente de programação que responde sempre em "
    "português brasileiro claro e direto. Você é técnico, objetivo e nunca "
    "alterna para inglês a menos que seja explicitamente pedido. Seus "
    "comentários em código também são em português brasileiro."
)


def _check_mlx_lm() -> None:
    try:
        import mlx_lm  # type: ignore  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        print(
            "[erro] 'mlx_lm' não instalado. Rode: pip install mlx-lm",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


def fuse_adapter(model: str, adapter_path: Path, save_path: Path) -> None:
    """Funde o adapter LoRA com o modelo base via ``mlx_lm.fuse``."""
    save_path.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "mlx_lm.fuse",
        "--model",
        model,
        "--adapter-path",
        str(adapter_path),
        "--save-path",
        str(save_path),
    ]
    print("[fuse] " + " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"[fuse] falhou (exit={result.returncode}).", file=sys.stderr)
        raise SystemExit(result.returncode)
    print(f"[fuse] modelo fundido em: {save_path}")


def convert_to_gguf(
    fused_path: Path,
    gguf_dir: Path,
    llama_cpp: Path,
    out_name: str,
    quantize: str | None,
) -> Path:
    """Converte o modelo fundido para GGUF usando o script do llama.cpp."""
    convert_script = llama_cpp / "convert_hf_to_gguf.py"
    if not convert_script.exists():
        # Compatibilidade com versões antigas do llama.cpp.
        legacy = llama_cpp / "convert-hf-to-gguf.py"
        if legacy.exists():
            convert_script = legacy
        else:
            print(
                f"[gguf] script convert_hf_to_gguf.py não achado em {llama_cpp}.\n"
                "       passe --llama-cpp apontando para um clone do llama.cpp.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    gguf_dir.mkdir(parents=True, exist_ok=True)
    out_file = gguf_dir / f"{out_name}.gguf"
    cmd = [
        sys.executable,
        str(convert_script),
        str(fused_path),
        "--outfile",
        str(out_file),
    ]
    if quantize:
        cmd += ["--outtype", quantize]
    print("[gguf] " + " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"[gguf] conversão falhou (exit={result.returncode}).", file=sys.stderr)
        raise SystemExit(result.returncode)

    print(f"[gguf] gerado: {out_file}")
    return out_file


def write_modelfile(
    gguf_path: Path,
    modelfile_path: Path,
    system_prompt: str,
    temperature: float,
    top_p: float,
    num_ctx: int,
) -> None:
    """Gera o ``Modelfile`` que o Ollama vai consumir."""
    content = (
        f"FROM {gguf_path}\n"
        "\n"
        f'SYSTEM """{system_prompt}"""\n'
        "\n"
        f"PARAMETER temperature {temperature}\n"
        f"PARAMETER top_p {top_p}\n"
        f"PARAMETER num_ctx {num_ctx}\n"
        'PARAMETER stop "<|im_start|>"\n'
        'PARAMETER stop "<|im_end|>"\n'
    )
    modelfile_path.write_text(content, encoding="utf-8")
    print(f"[ollama] Modelfile escrito em: {modelfile_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Funde LoRA, converte para GGUF e prepara modelo Ollama PT-BR."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="modelo base HF.")
    parser.add_argument("--adapter-path", type=Path, default=DEFAULT_ADAPTER)
    parser.add_argument("--fused-path", type=Path, default=DEFAULT_FUSED)
    parser.add_argument("--gguf-dir", type=Path, default=DEFAULT_GGUF_DIR)
    parser.add_argument(
        "--llama-cpp",
        type=Path,
        default=Path.home() / "dev" / "llama.cpp",
        help="diretório do clone do llama.cpp.",
    )
    parser.add_argument(
        "--ollama-name",
        default="mtzcode-pt",
        help="nome final do modelo no Ollama.",
    )
    parser.add_argument(
        "--quantize",
        default="q8_0",
        help="tipo de quantização GGUF (ex: f16, q8_0, q4_K_M). Vazio = sem quantizar.",
    )
    parser.add_argument("--modelfile", type=Path, default=DEFAULT_MODELFILE)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument(
        "--skip-fuse",
        action="store_true",
        help="pular o passo de fuse (já feito antes).",
    )
    parser.add_argument(
        "--skip-gguf",
        action="store_true",
        help="pular conversão GGUF (assume gguf-dir/{name}.gguf já existe).",
    )
    args = parser.parse_args()

    print(f"[export] modelo base:    {args.model}")
    print(f"[export] adapter:        {args.adapter_path}")
    print(f"[export] fused dir:      {args.fused_path}")
    print(f"[export] gguf dir:       {args.gguf_dir}")
    print(f"[export] llama.cpp em:   {args.llama_cpp}")

    if not args.skip_fuse:
        _check_mlx_lm()
        fuse_adapter(args.model, args.adapter_path, args.fused_path)
    else:
        print("[fuse] pulado (--skip-fuse).")

    if not args.skip_gguf:
        gguf_path = convert_to_gguf(
            fused_path=args.fused_path,
            gguf_dir=args.gguf_dir,
            llama_cpp=args.llama_cpp,
            out_name=args.ollama_name,
            quantize=args.quantize or None,
        )
    else:
        gguf_path = args.gguf_dir / f"{args.ollama_name}.gguf"
        print(f"[gguf] pulado, usando {gguf_path}")

    write_modelfile(
        gguf_path=gguf_path,
        modelfile_path=args.modelfile,
        system_prompt=SYSTEM_PROMPT_PT,
        temperature=args.temperature,
        top_p=args.top_p,
        num_ctx=args.num_ctx,
    )

    has_ollama = shutil.which("ollama") is not None
    print()
    print("=" * 60)
    print("[ok] tudo pronto. Para registrar no Ollama, rode:")
    print()
    print(f"    ollama create {args.ollama_name} -f {args.modelfile}")
    print(f"    ollama run    {args.ollama_name}")
    print()
    if not has_ollama:
        print("[aviso] 'ollama' não encontrado no PATH — instale em https://ollama.com")
    print("=" * 60)


if __name__ == "__main__":
    main()
