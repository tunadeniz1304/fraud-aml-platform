# syntax=docker/dockerfile:1.7
# Anil3 fraud platformu — API / worker / simulator imajı.

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libgomp1: LightGBM OpenMP; fonts-dejavu-core: ŞİB PDF'inde Türkçe karakterler.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 fraud \
    && mkdir -p /app/var /app/logs \
    && chown -R fraud:fraud /app/var /app/logs /app/data /app/models
USER fraud

# RAG için yerel MiniLM (ONNX) modelini imaja önceden indir; ağ yoksa build
# yine başarılı olur ve çalışma anında deterministik hash gömmeye düşülür.
RUN python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as E; E()(['ısınma'])" \
    || echo "ONNX modeli indirilemedi - hash gomme kullanilacak"

ENV FRAUD_DB_PATH=/app/var/fraud_platform.db \
    VECTOR_DIR=/app/var/chromadb \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/health/ready || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
