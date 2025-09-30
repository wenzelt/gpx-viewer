FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Separate layer for deps
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
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
