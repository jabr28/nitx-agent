FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
USER nobody
