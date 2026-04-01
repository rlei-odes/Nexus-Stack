# =============================================================================
# Sling - Custom ARM64-compatible build
# =============================================================================
# Official slingdata/sling image doesn't support ARM64, so we download
# the pre-built ARM64 binary from GitHub releases.
# =============================================================================

FROM debian:bookworm-slim

# Install dependencies for downloading and running sling
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Download and install Sling ARM64 binary
RUN set -eux; \
    curl -L -o /tmp/sling_linux_arm64.tar.gz https://github.com/slingdata-io/sling-cli/releases/download/v1.5.13/sling_linux_arm64.tar.gz; \
    curl -L -o /tmp/checksums.txt https://github.com/slingdata-io/sling-cli/releases/download/v1.5.13/linux.arm64.checksums.txt; \
    cd /tmp && sha256sum -c checksums.txt; \
    tar xzf /tmp/sling_linux_arm64.tar.gz -C /usr/local/bin; \
    chmod +x /usr/local/bin/sling; \
    rm /tmp/sling_linux_arm64.tar.gz /tmp/checksums.txt

# Create workspace directory
RUN mkdir -p /workspace
WORKDIR /workspace

# Keep container running for CLI access
CMD ["sleep", "infinity"]
