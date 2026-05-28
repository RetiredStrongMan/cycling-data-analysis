# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS base

# Minimal runtime deps for Python wheels (pandas/scipy use prebuilt wheels, but
# pillow/matplotlib transitively need libgomp on slim images)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl libgomp1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first so they cache independently of code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn==23.0.0

# Copy application code. Data, .env, .venv, reports are .dockerignored.
COPY *.py ./
COPY templates ./templates
COPY static ./static

# data/ is a persistent volume mounted by Fly at /app/data. Pre-create the
# directory so the first request doesn't race against the worker.
RUN mkdir -p /app/data/streams

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    SESSION_COOKIE_SECURE=1

EXPOSE 8080

# 1 worker process + 8 threads. We deliberately do NOT use multiple gunicorn
# workers because worker.py's ThreadPoolExecutor and rate_limit's process-
# global token bucket must be shared across all request handlers. Multiple
# gunicorn workers would each have their own copies, racing on the SQLite DB
# and on the Strava rate-limit budget. To scale beyond what 8 threads handle,
# split web + worker into separate containers and use Redis-backed RQ.
CMD ["gunicorn", "--workers", "1", "--threads", "8", \
     "--bind", "0.0.0.0:8080", "--timeout", "120", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "--forwarded-allow-ips", "*", \
     "app:app"]
