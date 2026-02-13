FROM python:3.11-slim AS base

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /src/

# --- Worker Extras ---
FROM base AS worker
COPY requirements-worker.txt .
RUN pip install -r requirements-worker.txt