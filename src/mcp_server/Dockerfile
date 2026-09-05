FROM mcr.microsoft.com/devcontainers/python:3.11-bullseye AS base
WORKDIR /app

FROM base AS builder

# Copy uv binaries from astral-sh image
COPY --from=ghcr.io/astral-sh/uv:0.6.3 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Copy lock and project files first for caching
COPY uv.lock pyproject.toml /app/

# Install dependencies (frozen, no dev) using uv
# RUN --mount=type=cache,target=/root/.cache/uv \
#     uv sync --frozen --no-install-project --no-dev

RUN uv sync --frozen --no-install-project --no-dev
# Copy application code and install project dependencies
COPY . /app
#RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
RUN uv sync --frozen --no-dev

# Final stage
FROM base

WORKDIR /app

# Development environment for workspace_exec (the agents' terminal).
# We do NOT predict which commands agents run: we ship a usable toolchain and
# expose a generic shell. The base image (devcontainers/python:3.11-bullseye)
# ALREADY provides bash, git, curl, wget, jq, zip/unzip, openssh-client,
# build-essential (gcc/g++/make), procps, findutils, grep, sed and python —
# verified in the image, so re-installing them would be pure noise.
# Missing: Node/npm and the Azure CLI. NOTE: the devcontainer has `az` only
# because of its devcontainer *feature*, NOT the base image — this image is
# built from this Dockerfile alone, so without the block below `az` is exit=127
# in the Container App. The base ships a yarn apt source whose GPG key is no
# longer valid, which makes ANY `apt-get update` fail (exit 100) — drop that
# source first. Installed before the COPY so the layer caches.
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && bash -c "set -o pipefail; curl -fsSL https://deb.nodesource.com/setup_20.x | bash -" \
    && apt-get install -y --no-install-recommends nodejs ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -sLS https://packages.microsoft.com/keys/microsoft.asc \
    | gpg --dearmor -o /etc/apt/keyrings/microsoft.gpg \
    && chmod go+r /etc/apt/keyrings/microsoft.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ bullseye main" \
    > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends azure-cli \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version && az version -o tsv

COPY --from=builder /app /app
COPY --from=builder /bin/uv /bin/uv

# Set PATH to use venv created by uv
ENV PATH="/app/.venv/bin:$PATH"

# Create non-root user. AZURE_CONFIG_DIR: az guarda el login de la Managed
# Identity ahí; el proceso del server y los hijos de workspace_exec lo comparten.
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app \
    && chmod +x /app/entrypoint.sh
ENV AZURE_CONFIG_DIR=/home/app/.azure
USER app

# Expose port
EXPOSE 9000

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:9000/health || exit 1

# entrypoint: `az login --identity` (best-effort) y luego el server
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uv", "run", "python", "mcp_server.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9000"]
