FROM python:3.13-slim

WORKDIR /app

# Install build dependencies and curl for health checks
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        gcc \
        g++ \
        make \
        && \
    apt-get upgrade -y

# Install Python dependencies
COPY requirements.txt requirements.txt
RUN pip install --upgrade pip && \
    pip install --upgrade setuptools && \
    pip install --upgrade --no-cache-dir -r requirements.txt

# Remove build dependencies to reduce image size
RUN apt-get remove -y gcc g++ make && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy application files
COPY . .

# Create non-root user and set up directories
RUN groupadd -r appuser && useradd -r -g appuser appuser && \
    mkdir -p uploads processed && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose the port (documentation only, actual port set via APP_PORT env var)
EXPOSE 5003

# Add health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:${APP_PORT:-5003}/health || exit 1

# Start FastAPI app with Uvicorn
# Using shell form to allow environment variable substitution
CMD uvicorn app:app --host 0.0.0.0 --port ${APP_PORT:-5003}
