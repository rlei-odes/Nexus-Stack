FROM codercom/code-server:latest

USER root

# Install Python 3 and dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-venv python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager) - pinned version
RUN curl -LsSf https://astral.sh/uv/0.6.12/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

USER coder

# Install JupyterLab via uv (provides the jupyter CLI)
RUN uv tool install jupyterlab==4.3.6

# Add uv tool bin to PATH
ENV PATH="/home/coder/.local/bin:${PATH}"
