# --- Stage 1: Tailwind Builder ---
FROM node:20-slim AS builder
WORKDIR /build

# We need the HTML file to know which classes to keep
COPY static/index.html ./static/index.html

# Run the standalone Tailwind CLI to generate the minified CSS
# This scans index.html and outputs only the used styles to tailwind-dist.css
RUN npx tailwindcss -c /dev/null -i /dev/null -o ./static/tailwind-dist.css --content "./static/index.html" --minify

# --- Stage 2: Final App ---
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install uv package manager
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
 && pip install --no-cache-dir uv

# Separate layer for deps
COPY pyproject.toml .
RUN uv sync --no-dev --no-install-project

# App code
COPY . .

# Copy the compiled CSS from the builder stage
COPY --from=builder /build/static/tailwind-dist.css ./static/tailwind-dist.css

# Ensure uploads dir exists
RUN mkdir -p /app/uploads

# Create a non-root user (optional but recommended)
RUN useradd -u 10001 -m appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Uvicorn in-process is fine for this small app
CMD ["sh", "-c", "uv run uvicorn main:app --host ${UVICORN_HOST:-0.0.0.0} --port ${UVICORN_PORT:-8000}"]
