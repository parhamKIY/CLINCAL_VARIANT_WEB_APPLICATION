#!/bin/bash
set -e

# Ensure storage and data directories exist
mkdir -p /app/storage/logs \
         /app/storage/uploads \
         /app/storage/reports \
         /app/storage/database \
         /app/storage/evidence_repository \
         /app/data/cache \
         /app/data/hpo

# If running as root, ensure correct permissions and drop privileges to appuser
if [ "$(id -u)" = "0" ]; then
    chown -R appuser:appuser /app/storage /app/data
    exec gosu appuser "$@"
else
    exec "$@"
fi
