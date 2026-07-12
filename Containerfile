FROM fedora:latest

# Install Python, pip, git (for setuptools-scm), and network tools
RUN dnf install -y --setopt=install_weak_deps=False \
    python3 \
    python3-pip \
    git \
    curl \
    && python3 --version \
    && dnf clean all

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash vault

# Set working directory
WORKDIR /app

# Create application data directory
RUN mkdir -p /app/data/vault && chown -R vault:vault /app/data

# Define volumes
VOLUME ["/app/data"]

# Copy project files (including .git for setuptools-scm version detection)
COPY pyproject.toml README.md ./
COPY .git/ ./.git/
COPY src/ ./src/

# Install the package
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Switch to non-root user
USER vault

# Default environment variables
ENV VAULT_PATH=/app/data/vault
ENV VAULT_HOST=0.0.0.0
ENV VAULT_PORT=8002
# Persist web-interface settings (recent vaults, model choices) in the data volume
ENV TALKPIPE_VAULT_HOME=/app/data/vault-home
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH=/home/vault/.local/bin:$PATH

# Expose port
EXPOSE 8002

# Run the web application
CMD ["vault-server", "/app/data/vault", "--host", "0.0.0.0", "--port", "8002"]
