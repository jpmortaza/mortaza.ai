FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependências de sistema mínimas (libmagic para detecção de tipo de arquivo,
# git pra tool github.py). Sem build-essential pra manter a imagem leve.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY mtzcode ./mtzcode
COPY skills ./skills
COPY bin ./bin

RUN pip install --no-cache-dir -e .

# Diretório de workspaces dos contatos WhatsApp
RUN mkdir -p /app/workspace
VOLUME ["/app/workspace"]

EXPOSE 8000

# Webhook + UI no mesmo processo. Em produção use múltiplos workers atrás de
# um proxy se a carga crescer; pra um bot pessoal, 1 worker basta.
CMD ["uvicorn", "mtzcode.web.server:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
