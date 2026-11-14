# FloodGate: one central server (API, monitors, Context.dev + LLM calls, frontend and the fictional sites).
#   docker build -t floodgate .
#   docker run -p 8000:8000 -v floodgate-data:/app/data --env-file .env \
#     -e PUBLIC_BASE_URL=https://your.domain -e ADMIN_TOKEN=<secret> floodgate
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PORT=8000
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend ./backend
COPY frontend ./frontend
COPY scenario ./scenario
COPY fake_site ./fake_site
VOLUME /app/data
EXPOSE 8000
CMD ["sh", "-c", "exec uv run --no-sync uvicorn backend.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*' --timeout-graceful-shutdown 2"]
