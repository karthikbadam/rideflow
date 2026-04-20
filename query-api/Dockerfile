FROM ghcr.io/astral-sh/uv:0.8.4-python3.11-bookworm-slim AS build

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app


FROM python:3.11.9-slim

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=build /app /app

EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=20s \
  CMD python -c "import urllib.request,sys; \
urllib.request.urlopen('http://localhost:8001/health', timeout=2); sys.exit(0)" \
  || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
