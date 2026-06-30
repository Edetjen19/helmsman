# Orchestrator runs on Python 3.12 (host Python is broken; develop & test in here).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

# App code (compose bind-mounts the repo over this for live dev; the COPY keeps the
# image self-contained so it also runs without the mount).
COPY . .

EXPOSE 8000

# Default command; compose overrides per service (web vs worker).
CMD ["uvicorn", "src.ingest.app:app", "--host", "0.0.0.0", "--port", "8000"]
