# Build from the repo root:  docker build -f api/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
COPY core /app/core
COPY api /app/api
RUN pip install --no-cache-dir /app/core /app/api
WORKDIR /app/api
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn crisisgrid_api.main:app --host 0.0.0.0 --port 8000"]
