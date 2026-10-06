# The build context excludes .git (see .containerignore), so setuptools_scm
# cannot derive the package version. Pass the real version as a build arg:
#   podman build --build-arg APP_VERSION="$(python3 -m setuptools_scm)" .
# When unset, the image reports the fallback version 0.1.0.
ARG APP_VERSION=0.1.0

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

# Create application data directory and the documents mountpoint (empty
# unless a host folder is mounted over it)
RUN mkdir -p /app/data /documents && chown -R vault:vault /app/data

# Define volumes
VOLUME ["/app/data"]

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY --chmod=755 container/entrypoint.sh /usr/local/bin/vault-entrypoint

# Install the package, then remove pip from the image. pip is only needed
# for this one install, and pip >= 25 ships an SBOM of its *vendored* code
# (pip/_vendor/bom.cdx.json) that Trivy reports as installed setuptools and
# msgpack packages with known vulnerabilities, though nothing outside pip
# uses that code. Deliberately no `pip install --upgrade pip` first: that
# would leave a second pip copy under /usr/local that `dnf remove` cannot
# see.
ARG APP_VERSION
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_TALKPIPE_VAULT=${APP_VERSION}
RUN pip install --no-cache-dir . && \
    dnf remove -y python3-pip && \
    dnf clean all

# Switch to non-root user
USER vault

# Default environment variables.
# Deliberately no VAULT_PATH/VAULT_HOST/VAULT_PORT: nothing reads them. The
# host and port are fixed by CMD below, and which vault is open is decided by
# `--resume` plus the web interface, not by the environment.
# Persist web-interface settings (recent vaults, model choices) in the data volume
ENV TALKPIPE_VAULT_HOME=/app/data/vault-home
# Keep the Hugging Face model cache in the data volume so the embedding model
# is downloaded once, on first use, and survives container recreation (run the
# container with /app/data on a volume, as the documented commands do).
ENV HF_HOME=/app/data/hf-cache
# Cap per-file Hugging Face metadata checks (default 10s) so online mode
# degrades to the cached model quickly when huggingface.co is slow.
ENV HF_HUB_ETAG_TIMEOUT=5
# Cap glibc malloc arenas: LanceDB's multithreaded writer otherwise strands
# freed memory across per-thread arenas, so long ingestions grow RSS without
# bound and OOM-kill the container (see talkpipe_vault/memtune.py).
ENV MALLOC_ARENA_MAX=2
# Fence the web interface into container-appropriate paths: vaults may only
# live in the persistent data volume, and only the mounted documents tree can
# be browsed or indexed. Unset (empty) means unrestricted.
ENV TALKPIPE_VAULT_ROOT=/app/data
ENV TALKPIPE_DOCUMENT_ROOTS=/documents
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH=/home/vault/.local/bin:$PATH

# Expose port
EXPOSE 8002

# Auto-detect Hugging Face reachability, then run the web application.
# --resume reopens the vault last used in the web interface; before any vault
# has been opened, the UI starts on the Vaults page.
ENTRYPOINT ["vault-entrypoint"]
CMD ["vault-server", "--resume", "--host", "0.0.0.0", "--port", "8002", "--no-browser"]
