FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev
COPY rules ./rules
EXPOSE 8000
CMD ["uv", "run", "--no-dev", "fre", "serve", "--host", "0.0.0.0", "--port", "8000", "--db", "/data/fre.sqlite"]
