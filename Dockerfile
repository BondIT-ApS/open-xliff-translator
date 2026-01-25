FROM python:3.14-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt requirements.txt
RUN pip install --upgrade pip && \
    pip install --upgrade setuptools && \
    pip install --upgrade --no-cache-dir -r requirements.txt

# Update packages and install any additional packages for DEV
RUN apt-get update && \
    apt-get upgrade -y && \
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

# Expose the port
EXPOSE 5003

# Start Flask app
CMD ["python", "app.py"]
