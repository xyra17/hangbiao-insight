FROM python:3.12-slim
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock /app/backend/
RUN pip install --no-cache-dir uv && cd /app/backend && uv sync --no-dev
COPY backend /app/backend
ENV PYTHONPATH=/app/backend EXECUTION_MODE=deterministic_local
EXPOSE 8200
CMD ["/app/backend/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200"]
