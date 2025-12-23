# Multi-stage build for Vault
# Stage 1: Build stage with all development dependencies
FROM fedora:latest AS builder

# Install system dependencies for building
RUN dnf update -y && \
    dnf install -y \
        python3 \
        python3-pip \
        python3-devel \
        git \
        gcc \
        gcc-c++ \
        make \
        cmake \
        pkg-config \
        libxml2-devel \
        libxslt-devel \
        openssl-devel \
        libglvnd-glx \
        mesa-libGL \
        && dnf clean all

# Create build user
RUN groupadd -r builder && useradd -r -g builder -m builder

# Set up build environment
WORKDIR /build
RUN chown builder:builder /build && \
    mkdir -p /tmp/numba_cache && \
    chmod 777 /tmp/numba_cache
USER builder

# Set numba cache directory for build stage
ENV NUMBA_CACHE_DIR=/tmp/numba_cache

# Copy source files
COPY --chown=builder:builder pyproject.toml README.md LICENSE ./
COPY --chown=builder:builder src/ src/
COPY --chown=builder:builder tests/ tests/

# Install Python dependencies and build the package
RUN python3 -m pip install --user --upgrade pip setuptools wheel build
# Pre-install numpy with pre-built wheels to avoid compilation issues
RUN python3 -m pip install --user --only-binary=:all: numpy
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_TALKPIPE_VAULT=0.1.0
RUN python3 -m pip install --user -e .[dev,docling]
# Run tests - they will be skipped if ollama is not available
# If ollama is available, tests must pass or the build fails
RUN python3 -m pytest tests/ --log-cli-level=INFO
RUN python3 -m build --wheel

# Stage 2: Runtime stage with minimal dependencies
FROM fedora:latest AS runtime

# Install runtime system dependencies including docling requirements
RUN dnf update -y && \
    dnf install -y \
        python3 \
        python3-pip \
        git \
        libxml2 \
        libxslt \
        libglvnd-glx \
        mesa-libGL \
        && dnf clean all && \
        rm -rf /var/cache/dnf

# Create application user with specific UID/GID for better security
RUN groupadd -r -g 1001 app && \
    useradd -r -u 1001 -g app -s /sbin/nologin \
        -c "Vault Application User" app

# Set up application directory
WORKDIR /app
RUN mkdir -p /app/data /app/watch /tmp/numba_cache && \
    chown -R app:app /app && \
    chmod 777 /tmp/numba_cache

# Copy the built wheel from builder stage
COPY --from=builder --chown=app:app /build/dist/*.whl /tmp/

# Install runtime Python dependencies with binary wheels where possible
RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir --only-binary=:all: numpy && \
    python3 -m pip install --no-cache-dir accelerate && \
    python3 -m pip install --no-cache-dir docling && \
    python3 -m pip install --no-cache-dir /tmp/*.whl && \
    rm -f /tmp/*.whl

# Copy only necessary runtime files
COPY --chown=app:app pyproject.toml ./

# Copy entrypoint script
COPY --chown=app:app entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create volume mount points for watch and vault directories
VOLUME ["/watch", "/vault"]

# Switch to non-root user
USER app

# Expose the application port
EXPOSE 8002

# Health check to ensure the application starts correctly
# Checks that Python imports work
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import talkpipe_vault; print('OK')" || exit 1

# Set environment variables for better container behavior
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_PREFER_BINARY=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NUMBA_CACHE_DIR=/tmp/numba_cache \
    VAULT_PATH=/vault \
    VAULT_WATCH_DIR=/watch \
    VAULT_HOST=0.0.0.0 \
    VAULT_PORT=8002

# Default command runs both watcher and web app
CMD ["/app/entrypoint.sh"]