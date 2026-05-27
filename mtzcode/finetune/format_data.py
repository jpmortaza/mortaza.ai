"""Converte os datasets brutos PT-BR para o formato ShareGPT.

Lê tudo de ``~/.mtzcode/finetune/raw/*.jsonl`` e gera::

    ~/.mtzcode/finetune/formatted/train.jsonl
    ~/.mtzcode/finetune/formatted/valid.jsonl

Cada linha é um exemplo no formato ShareGPT::

    {"conversations": [
        {"from": "human", "value": "..."},
        {"from": "gpt",   "value": "..."}
    ]}

Para datasets não-conversacionais (Wikipedia, OSCAR), gera pares sintéticos do
tipo "Explique X em português" → conteúdo original.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any, Iterable

RAW_DIR = Path.home() / ".mtzcode" / "finetune" / "raw"
FORMATTED_DIR = Path.home() / ".mtzcode" / "finetune" / "formatted"

# Templates simples para gerar prompts sintéticos a partir de texto puro.
SYNTHETIC_PROMPTS_WITH_TITLE = [
    "Explique em português o que é {title}.",
    "Me fale sobre {title} em português brasileiro.",
    "O que você sabe sobre {title}? Responda em português.",
    "Resuma o assunto '{title}' em português brasileiro.",
    "Descreva {title} de forma clara e em português.",
]

SYNTHETIC_PROMPTS_NO_TITLE = [
    "Reescreva o texto a seguir em português brasileiro claro:\n\n{snippet}",
    "Resuma em português o seguinte trecho:\n\n{snippet}",
    "Continue este texto em português brasileiro:\n\n{snippet}",
]


def _ensure_dirs() -> None:
    FORMATTED_DIR.mkdir(parents=True, exist_ok=True)


def _sharegpt(human: str, gpt: str) -> dict[str, Any]:
    return {
        "conversations": [
            {"from": "human", "value": human.strip()},
            {"from": "gpt", "value": gpt.strip()},
        ]
    }


def _iter_raw_file(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _format_qa(row: dict[str, Any]) -> dict[str, Any] | None:
    """Formata um item de dataset QA (Pirá, own_logs)."""
    q = (row.get("question") or "").strip()
    a = (row.get("answer") or "").strip()
    if not q or not a:
        return None
    return _sharegpt(q, a)


def _format_text(row: dict[str, Any], rng: random.Random) -> dict[str, Any] | None:
    """Gera par sintético para texto puro (wiki, OSCAR)."""
    text = (row.get("text") or "").strip()
    if len(text) < 80:
        return None
    title = (row.get("title") or "").strip()
    snippet = text[:1500]
    if title:
        prompt = rng.choice(SYNTHETIC_PROMPTS_WITH_TITLE).format(title=title)
    else:
        prompt = rng.choice(SYNTHETIC_PROMPTS_NO_TITLE).format(snippet=snippet[:300])
    return _sharegpt(prompt, snippet)


def build_examples(rng: random.Random) -> list[dict[str, Any]]:
    """Lê todos os raw files e devolve a lista de exemplos formatados."""
    examples: list[dict[str, Any]] = []

    if not RAW_DIR.exists():
        print(
            f"[erro] {RAW_DIR} não existe. Rode primeiro: "
            "python -m mtzcode.finetune.collect_data",
            file=sys.stderr,
        )
        raise SystemExit(1)

    raw_files = sorted(RAW_DIR.glob("*.jsonl"))
    if not raw_files:
        print(f"[erro] nenhum .jsonl em {RAW_DIR}", file=sys.stderr)
        raise SystemExit(1)

    for raw_file in raw_files:
        name = raw_file.stem
        count = 0
        for row in _iter_raw_file(raw_file):
            ex: dict[str, Any] | None
            if "question" in row and "answer" in row:
                ex = _format_qa(row)
            elif "text" in row:
                ex = _format_text(row, rng)
            else:
                ex = None
            if ex is not None:
                examples.append(ex)
                count += 1
        print(f"[format] {name}: {count} exemplos")

    rng.shuffle(examples)
    return examples


def split_train_valid(
    examples: list[dict[str, Any]], val_ratio: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    n_val = max(1, int(len(examples) * val_ratio))
    return examples[n_val:], examples[:n_val]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Formata raw PT-BR para ShareGPT (train/valid)."
    )
    parser.add_argument(
        "--val-ratio", type=float, default=0.05, help="fração para validação (0.05 = 5%%)."
    )
    parser.add_argument("--seed", type=int, default=42, help="seed do shuffle.")
    args = parser.parse_args()

    _ensure_dirs()
    rng = random.Random(args.seed)

    examples = build_examples(rng)
    if not examples:
        print("[erro] nenhum exemplo válido foi gerado.", file=sys.stderr)
        raise SystemExit(1)

    train, valid = split_train_valid(examples, args.val_ratio)

    train_path = FORMATTED_DIR / "train.jsonl"
    valid_path = FORMATTED_DIR / "valid.jsonl"

    n_train = _write_jsonl(train_path, train)
    n_valid = _write_jsonl(valid_path, valid)

    print(f"[format] {n_train} exemplos → {train_path}")
    print(f"[format] {n_valid} exemplos → {valid_path}")
    print("[format] concluído.")


if __name__ == "__main__":
    main()
