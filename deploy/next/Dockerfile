FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build

WORKDIR /app
RUN python -m pip install --no-cache-dir uv==0.12.14

COPY .python-version pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra server --no-install-project

COPY README.md LICENSE COMMERCIAL-LICENSE.md ./
COPY src/ src/
RUN uv sync --locked --no-dev --extra server --no-editable

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=build /app/.venv/ .venv/
USER 10001:10001
EXPOSE 8100
CMD ["python", "-m", "mnemotrace_next.server", "--host", "0.0.0.0"]
