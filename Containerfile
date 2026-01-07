FROM fedora:latest

# Install Python, pip, git (for setuptools-scm), Java (for Tika), and network tools
RUN dnf install -y --setopt=install_weak_deps=False \
    python3 \
    python3-pip \
    git \
    java-21-openjdk-headless \
    curl \
    iputils \
    bind-utils \
    && python3 --version \
    && java -version \
    && dnf clean all

# Create non-root user
RUN useradd -m -u 1000 -s /bin/bash vault

# Set working directory
WORKDIR /app

# Copy project files (including .git for setuptools-scm version detection)
COPY pyproject.toml README.md ./
COPY .git/ ./.git/
COPY src/ ./src/

# Install the package
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .[tika]

# Copy entrypoint script
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Switch to non-root user
USER vault

# Default environment variables
ENV VAULT_PATH=/vault
ENV VAULT_WATCH_DIR=/watch
ENV VAULT_HOST=0.0.0.0
ENV VAULT_PORT=8002

# Expose port
EXPOSE 8002

# Run entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]

