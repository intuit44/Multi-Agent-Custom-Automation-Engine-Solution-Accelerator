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
# Only Node/npm are missing, and the base ships a yarn apt source whose GPG key
# is no longer valid, which makes ANY `apt-get update` fail (exit 100) — drop
# that source first. Installed before the COPY so the layer caches.
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && bash -c "set -o pipefail; curl -fsSL https://deb.nodesource.com/setup_20.x | bash -" \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version

COPY --from=builder /app /app
COPY --from=builder /bin/uv /bin/uv

# Set PATH to use venv created by uv
ENV PATH="/app/.venv/bin:$PATH"

# Create non-root user
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app
USER app

# Expose port
EXPOSE 9000

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:9000/health || exit 1

# Run your main script
CMD ["uv", "run", "python", "mcp_server.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9000"]
