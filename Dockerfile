# ==============================================================================
# Clinical Variant Interpretation Application - Production Dockerfile
# ==============================================================================
FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Install essential system dependencies:
# - curl: for container healthcheck
# - fontconfig & fonts-dejavu-core: for PDF and Word document rendering
# - gosu: for safely stepping down from root to appuser in entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    fontconfig \
    fonts-dejavu-core \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Create application user and group (non-root security principle)
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g appuser -m -s /bin/bash appuser

WORKDIR /app

# Install Python dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code, assets, and templates
COPY . .

# Ensure storage directories exist, set ownership and executable permissions
RUN mkdir -p /app/storage/logs \
             /app/storage/uploads \
             /app/storage/reports \
             /app/storage/database \
             /app/storage/evidence_repository \
             /app/data/cache \
             /app/data/hpo && \
    chmod +x /app/entrypoint.sh && \
    chown -R appuser:appuser /app

# Expose Streamlit default port
EXPOSE 8501

# Container healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl --fail http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
