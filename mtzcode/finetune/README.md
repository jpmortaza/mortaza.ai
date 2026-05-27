# Fine-tuning LoRA em PT-BR (MLX)

Este módulo contém o pipeline completo para fazer fine-tuning LoRA do modelo
base do `mtzcode` em **português brasileiro**, rodando 100% local em Apple
Silicon via [MLX](https://github.com/ml-explore/mlx).

## Por que fine-tunar?

O modelo base (Qwen2.5-14B-Instruct, por exemplo) já fala português, mas tende
a escorregar para inglês em respostas técnicas, usar regionalismos de Portugal,
ou perder naturalidade quando o assunto é código. Um LoRA leve treinado em
corpus PT-BR de qualidade resolve isso sem precisar retrainar o modelo inteiro:

- Melhora fluência e vocabulário **brasileiro**.
- Reduz code-switching para inglês em respostas longas.
- Permite injetar **estilo do mtzcode** (respostas diretas, prompts em PT-BR,
  comentários em PT-BR no código gerado).
- Mantém todo o conhecimento do modelo base intacto (LoRA é aditivo).

## Hardware necessário

- Mac Apple Silicon (M1 / M2 / M3 / M4).
- **16 GB de RAM unificada no mínimo** para um 7B; 32 GB+ recomendado para 14B.
- ~30 GB livres em disco (modelo base + datasets + adapters + GGUF).
- macOS 13.5+ (necessário pelo MLX).

## Pipeline em 5 passos

### 1. Instalar dependências

```bash
pip install mlx-lm datasets
```

Opcionalmente, para conversão GGUF e import no Ollama:

```bash
git clone https://github.com/ggerganov/llama.cpp ~/dev/llama.cpp
pip install -r ~/dev/llama.cpp/requirements.txt
```

### 2. Coletar dados PT-BR

```bash
python -m mtzcode.finetune.collect_data
```

Baixa os datasets em `~/.mtzcode/finetune/raw/`. Datasets recomendados:

- **CarolinaBR** — corpus geral PT-BR de alta qualidade.
- **OSCAR PT** (`oscar-corpus/OSCAR-2301`, subset `pt`) — web crawl filtrado.
- **Wikipedia PT** (`wikipedia/20220301.pt`) — conhecimento enciclopédico.
- **Pirá** (`paulopirozelli/pira`) — perguntas e respostas em PT-BR.
- **Seus próprios logs** — `~/.mtzcode/logs/*.jsonl` do `session_log` viram
  pares user→assistant automaticamente (estilo do mtzcode preservado).

### 3. Formatar para ShareGPT

```bash
python -m mtzcode.finetune.format_data
```

Converte tudo para formato ShareGPT (`{"conversations": [...]}`), gera pares
sintéticos para datasets não-conversacionais (wiki, OSCAR) e salva
`train.jsonl` / `valid.jsonl` (split 95/5) em `~/.mtzcode/finetune/formatted/`.

### 4. Treinar o LoRA

```bash
python -m mtzcode.finetune.train_lora \
    --model Qwen/Qwen2.5-14B-Instruct \
    --iters 1000 \
    --batch-size 2 \
    --lora-layers 16
```

Os adapters saem em `~/.mtzcode/finetune/adapters/`. Tempo estimado num **M4
Pro com 36 GB**:

| Modelo | Iters | Tempo aproximado |
|--------|-------|------------------|
| 7B     | 1000  | ~2 h             |
| 14B    | 1000  | ~5 h             |
| 14B    | 2000  | ~8 h             |

### 5. Exportar para Ollama

```bash
python -m mtzcode.finetune.export_ollama \
    --model Qwen/Qwen2.5-14B-Instruct \
    --adapter-path ~/.mtzcode/finetune/adapters \
    --llama-cpp ~/dev/llama.cpp
```

Faz o **fuse** do adapter no modelo base, converte para GGUF e gera um
`Modelfile` pronto. Por fim, basta rodar:

```bash
ollama create mtzcode-pt -f Modelfile
ollama run mtzcode-pt
```

## Volume mínimo

- **1k–5k exemplos** de qualidade já mudam bastante o comportamento do modelo.
- Mais não é necessariamente melhor: prefira curadoria a quantidade.
- Misturar 50% PT-BR genérico + 50% logs próprios costuma dar o melhor
  equilíbrio entre fluência e estilo do `mtzcode`.

## Dicas

- Comece com `--iters 200` só para validar o pipeline.
- Acompanhe `train.log` para detectar overfit (val loss subindo).
- Faça backup de `~/.mtzcode/finetune/adapters/` antes de re-treinar.
