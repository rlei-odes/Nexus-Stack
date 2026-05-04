# =============================================================================
# Soda Core - Multi-arch build (amd64 + arm64)
# =============================================================================
# Official sodadata/soda-core image is amd64-only. We build our own from
# `python:3.11-slim` (multi-arch base) + pip install — pip serves
# arch-correct wheels from PyPI, so the resulting image is correct on
# whichever architecture it's built on. Originally created for ARM
# parity; now also covers the post-2026-05 x86 (cpx32) default.
# =============================================================================

FROM python:3.11-slim

# Install system dependencies required by Soda Core
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Soda Core with PostgreSQL support
RUN pip install --no-cache-dir \
    soda-core-postgres==3.3.7

# Create workspace directory
RUN mkdir -p /workspace
WORKDIR /workspace

# Default command (sleep infinity for CLI access)
CMD ["sleep", "infinity"]
