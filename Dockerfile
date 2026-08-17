# Northflank build target. Render deploys from render.yaml's buildCommand/
# startCommand instead (no Dockerfile needed there) -- this file is additive,
# not a replacement, so the Render deploy is unaffected by its presence.
FROM python:3.11-slim

WORKDIR /app

# Split from the COPY . . below so Docker's layer cache only reinstalls
# dependencies when requirements.txt actually changes, not on every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Northflank auto-detects this EXPOSE and routes traffic to it -- no dynamic
# $PORT env var to read at runtime, unlike Render's convention.
EXPOSE 8080

# Same gunicorn config as render.yaml's startCommand -- see that file for why
# threads=16 (one thread pinned per open SSE connection).
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "16", "--timeout", "120"]
