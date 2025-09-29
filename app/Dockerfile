FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# System deps required for psycopg2 and runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Separate layer for deps
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Ensure uploads dir exists
RUN mkdir -p /app/uploads

# Create a non-root user (optional but recommended)
RUN useradd -u 10001 -m appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Uvicorn in-process is fine for this small app
CMD ["sh", "-c", "uvicorn main:app --host ${UVICORN_HOST:-0.0.0.0} --port ${UVICORN_PORT:-8000}"]
