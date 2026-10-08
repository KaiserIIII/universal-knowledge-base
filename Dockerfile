FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS core
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY backend/requirements-core.lock /app/backend/requirements-core.lock
RUN python -m pip install --no-cache-dir -r backend/requirements-core.lock
COPY backend/app /app/backend/app
COPY backend/run.py /app/backend/run.py
COPY web /app/web
COPY index.html /app/index.html
RUN useradd --system --uid 10001 --home-dir /app --no-create-home knowledge \
    && mkdir -p /app/backend/app/data/saas \
    && chown -R 10001:10001 /app/backend/app/data
ENV HOST=0.0.0.0 PORT=8000 HF_HOME=/app/backend/app/data/model-cache
EXPOSE 8000
WORKDIR /app/backend
USER 10001:10001
CMD ["python", "run.py"]

# Full local RAG/parser stack. Core is a useful API-only target for external
# adapters and offline tests; the default image includes local retrieval.
FROM core AS rag
USER root
COPY backend/requirements-rag.lock /app/backend/requirements-rag.lock
RUN python -m pip install --no-cache-dir -r /app/backend/requirements-rag.lock
USER 10001:10001
