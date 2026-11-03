# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS console-build
WORKDIR /build/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm run build

FROM python:3.15-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MNEMOTRACE_DATA_DIR=/data \
    MNEMOTRACE_EMBEDDER=hashing \
    MNEMOTRACE_MAX_REQUEST_BYTES=2097152

WORKDIR /app

RUN groupadd --gid 10001 mnemotrace \
    && useradd --uid 10001 --gid mnemotrace --home-dir /nonexistent --shell /usr/sbin/nologin mnemotrace \
    && install -d -o mnemotrace -g mnemotrace /data

COPY pyproject.toml README.md LICENSE COMMERCIAL-LICENSE.md ./
COPY mnemotrace/ ./mnemotrace/
RUN python -m pip install ".[server,mcp,llm,qdrant]"

COPY --from=console-build /build/frontend/dist/ ./frontend/dist/

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
    CMD ["python", "-c", "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3); assert r.status == 200 and json.load(r)['ready']"]

CMD ["python", "-m", "uvicorn", "mnemotrace.server.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
