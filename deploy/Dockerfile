FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS build

WORKDIR /app
RUN python -m pip install --no-cache-dir uv==0.12.14

COPY .python-version pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra server --no-install-project

COPY README.md LICENSE ./
COPY mnemotrace/ mnemotrace/
RUN uv sync --locked --no-dev --extra server --no-editable

FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=build /app/.venv/ .venv/
USER 10001:10001
EXPOSE 8100
CMD ["python", "-m", "mnemotrace.server", "--host", "0.0.0.0"]
