# Build from the repo root:  docker build -f api/Dockerfile .      (NOT yet verified: Docker was unavailable when this was written)
# Stage 1 builds the notebook UI that the API serves under /paper-env/ui/
FROM node:22-slim AS ui
WORKDIR /src/paper_env/frontend
COPY paper_env/frontend/package.json paper_env/frontend/package-lock.json ./
RUN npm ci
COPY paper_env/frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY core /app/core
COPY paper_env /app/paper_env
COPY api /app/api
COPY --from=ui /src/paper_env/paper_env/api/static/app /app/paper_env/paper_env/api/static/app
# uv is needed at runtime: paper_env builds one virtualenv per paper with it
RUN pip install --no-cache-dir uv /app/core /app/paper_env /app/api
WORKDIR /app/api
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn crisisgrid_api.main:app --host 0.0.0.0 --port 8000"]
