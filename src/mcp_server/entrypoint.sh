#!/usr/bin/env bash
# Entrypoint del MCP server (Container App).
#
# workspace_exec expone un shell real a los agentes; para que `az ...` funcione
# ahí, la CLI tiene que estar AUTENTICADA en este contenedor. En Container App
# no hay `az login` interactivo: se inicia sesión con la Managed Identity del
# contenedor (user-assigned si AZURE_CLIENT_ID/CLIENT_ID está definido, si no la
# system-assigned). Best-effort: si falla (p. ej. local sin IMDS) el server
# arranca igual y `az` reportará "Please run az login" en el propio comando.
set -euo pipefail

if command -v az >/dev/null 2>&1; then
  if [[ -n "${IDENTITY_ENDPOINT:-}" || -n "${MSI_ENDPOINT:-}" ]]; then
    client_id="${AZURE_CLIENT_ID:-${CLIENT_ID:-}}"
    if [[ -n "$client_id" ]]; then
      az login --identity --client-id "$client_id" --allow-no-subscriptions -o none \
        && echo "[entrypoint] az login --identity ($client_id) OK" \
        || echo "[entrypoint] az login --identity failed (continuing)" >&2
    else
      az login --identity --allow-no-subscriptions -o none \
        && echo "[entrypoint] az login --identity (system-assigned) OK" \
        || echo "[entrypoint] az login --identity failed (continuing)" >&2
    fi
  else
    echo "[entrypoint] no managed identity endpoint; skipping az login"
  fi
fi

exec "$@"
