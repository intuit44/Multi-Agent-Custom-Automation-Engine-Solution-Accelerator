FROM mcr.microsoft.com/devcontainers/python:3.11-bullseye

# Remove Yarn repository to avoid GPG key expiration issue
RUN rm -f /etc/apt/sources.list.d/yarn.list

# hadolint — Dockerfile linter (used by exiasr.hadolint VS Code extension)
# Releases use x86_64 / arm64, not dpkg's amd64 / arm64
RUN ARCH="$(uname -m)" \
    && curl -fsSL "https://github.com/hadolint/hadolint/releases/latest/download/hadolint-Linux-${ARCH}" \
       -o /usr/local/bin/hadolint \
    && chmod +x /usr/local/bin/hadolint
