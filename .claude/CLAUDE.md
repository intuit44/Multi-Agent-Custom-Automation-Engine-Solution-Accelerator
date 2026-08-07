# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> The active backend is the **v4** stack built on Microsoft **Agent Framework** (`agent_framework` + `agent_framework_orchestrations`), invoking **Azure AI Foundry** agents. Anything referencing **Semantic Kernel**, `app_kernel:app`, a "Planner Agent", or MCP on `:8001` is from the pre-migration infra — treat it as stale, not current.

## Components & ports

Three independently runnable pieces (`src/`):

| Component | Path | Dev run | Port |
|-----------|------|---------|------|
| Backend (FastAPI) | `src/backend` | `uv run uvicorn app:app --port 8000 --reload` | 8000 |
| Frontend (React 18 + Vite + FluentUI v9, Redux) | `src/frontend` | `npm run dev` | 3001 |
| MCP server (FastMCP) | `src/mcp_server` | `uv run python mcp_server.py --transport streamable-http` | 9000 (`/mcp`) |

The backend reaches the MCP server via the `MCP_SERVER_ENDPOINT` env var (e.g. `http://localhost:9000/mcp`), not a hardcoded port.

## Commands

```bash
# Backend (uv-managed: pyproject.toml + uv.lock)
cd src/backend && uv run uvicorn app:app --port 8000 --reload

# Tests — SIEMPRE con el contrato del backend (--project). Un `uv run pytest`
# pelado desde la raíz cae en un venv obsoleto (PATH-fallback silencioso).
uv run --project src/backend pytest -m "not integration"   # canónico (885 tests)
uv run --project src/backend pytest src/tests/backend/test_x.py::test_y -v
# markers: unit, integration (live Azure), e2e (Playwright, needs running frontend)
# pytest.ini convierte ResourceWarning/PytestUnraisableExceptionWarning en errores
# (gate de fugas async) — no silenciarlos, cerrar la fuga.

# Lint/format/types — POR COMPONENTE (venvs distintos; ruff cap <0.16 en ambos)
cd src/backend && uv run ruff check . && uv run ruff format --check . && uv run mypy .
cd src/mcp_server && uv run --no-sync ruff check .   # (tras uv sync --extra dev)

# Frontend
cd src/frontend && npm run dev      # vite :3001
npm run build                        # tsc && vite build
npm run test                         # vitest
npm run lint                         # eslint src --ext .js,.jsx,.ts,.tsx
```

## Configuration & auth (read before touching credentials)

- Single config singleton: `common/config/app_config.py` → `config`. `config.APP_ENV` (`"dev"` | `"prod"`) gates credential behavior.
  - `prod` → Managed Identity (`ManagedIdentityCredentialAsync`).
  - `dev` → `DefaultAzureCredentialAsync(exclude_environment_credential=True)` → uses your `az login`. **Env-var service principals are ignored in dev** because of that exclusion.
- **Data-plane (Foundry) audience is `https://ai.azure.com`.** A `az login` token works only if the principal has the **Azure AI Developer** role on the Foundry account; a *guest/external* (`#EXT#`) identity is rejected even with the right audience.
- **OBO:** `config.build_user_credential(token)` returns a real `OnBehalfOfCredential` when `ENABLE_OBO` is set (needs `OBO_CLIENT_ID` + secret/cert), otherwise a `StaticTokenCredential` that **forwards the EasyAuth token verbatim** — which has the wrong audience for the Foundry data plane in local dev.
- Process-scoped shared resources: `config.get_shared_async_credential()` and `config.get_ai_project_client()` are created once and closed once in the FastAPI `lifespan` (`app.py` → `config.aclose_shared_resources()`). Agents **borrow** these; they must not enter them into a per-agent `AsyncExitStack` or close them.
- API endpoints never read headers directly. Use `_extract_auth(request) -> (user_id, tenant_id)` or `_extract_auth_with_token(request) -> (user_id, tenant_id, access_token)` in `v4/api/router.py`.

## Backend architecture (v4)

All v4 routes live in one large file: `v4/api/router.py`. There are **two distinct request paths** — know which one you're in:

**1. Direct conversational chat** — `POST /api/v4/chat/message/stream` (SSE).
- Intent is classified (`v4/orchestration/intent_router.py`); conversational/MCP intents answer inline.
- Builds a throwaway workflow via `_create_direct_response_workflow`, streams via `agent.invoke`, and **closes those agents at the end of the request**???

**2. Formal plan / multi-agent task** — `POST /api/v4/process_request` (+ `init_team`, `resume_plan`, `plan_approval`).
- Builds/loads a **session-scoped** orchestration via `OrchestrationManager.get_current_or_new_orchestration`, then runs `OrchestrationManager.run_orchestration` as a **FastAPI BackgroundTask** (returns HTTP immediately; work continues after the response). Progress streams to the frontend over **WebSocket** (`WebsocketMessageType` events), not SSE.

### OrchestrationManager (`v4/orchestration/orchestration_manager.py`)
- The Magentic **workflow graph is single-use** (cannot be reused after a run completes) → rebuilt for every new task / team switch.
- **Agents are reused** across tasks/approvals of the same team; they are only torn down on a genuine **team switch** or app shutdown — never per task. (Closing them per task would tear down network clients/credentials a still-running BackgroundTask or pending approval depends on.)
- The manager wraps a `HumanApprovalMagenticManager`; approvals/clarifications are awaited via `orchestration_config` events (`v4/config/settings.py`).

### Agents (`v4/magentic_agents/`)
- `MagenticAgentFactory.get_agents` builds one `FoundryAgentTemplate` per entry in a team config. Teams are JSON in `data/agent_teams/*.json` (`TeamConfiguration`); per-agent flags: `use_rag` (Azure AI Search), `use_mcp`, `coding_tools` (code interpreter), `use_reasoning`.
- `FoundryAgentTemplate` (`foundry_agent.py`) picks the client by capability:
  - **`AzureAIClient`** — published Foundry agent (`use_latest_version=True`), server-side tools (KB, code interpreter). Used when there are no runtime tools.
  - **`AzureOpenAIResponsesClient`** — when runtime MCP tools are attached.
- Foundry agent definitions are **reused by name** (`_FOUNDRY_REGISTERED_AGENT_NAMES` in `common/lifecycle.py`) to avoid version sprawl; set `MACAE_FORCE_AGENT_PUBLISH=1` to force a new published version.
- Lifecycle base: `MCPEnabledBase` (`magentic_agents/common/lifecycle.py`) owns the per-run `AsyncExitStack`, the MCP tool, and (only) a per-user OBO credential it created.

### MCP — how agents reach external servers
- **Agent runtime MCP** comes from `MCPConfig.from_env()` → the single `MCP_SERVER_ENDPOINT` server (**MacaeMcpServer**, the `ca-mcp` container). That is the only MCP endpoint an agent built by the factory calls directly.
- **The connector registry IS reachable by agents — indirectly, through MacaeMcpServer's tools.** MacaeMcpServer exposes `connect_from_registry` / `connect_mcp_server` / `discover_mcp_capabilities` / `call_external_tool` (`src/mcp_server/services/inspector_service.py`). An agent calls these; they look up the server in the Cosmos `mcp_connections` catalog (`MCPConnectionsService`, populated by the UI "Aplicaciones" registry `POST /mcp/connections/...`) and connect to the external server on the agent's behalf. So registering a server in the UI **does** make it usable — via these tools, not as a directly-bound agent tool. (Servers bound in the **Foundry portal** are a separate path: they reach agents because they're attached server-side to the published Foundry agent.)

### MCP credential model — `auth_type` vs `credential_source` (`MCPServerEntry`)
The catalog separates two concepts (`src/backend/v4/common/models/mcp_connection_models.py`):
- **`auth_type`** = what the target server sees on the wire — almost always `bearer_token` (standard MCP).
- **`credential_source`** = HOW MacaeMcpServer obtains a valid token. One dispatcher — `credential_resolver.resolve_valid_token(credential_source, audience, secret_ref)` in `src/mcp_server/credential_resolver.py` (self-contained twin, NOT the backend one) — handles all three:
  - `static_secret` — fixed token from Key Vault. Dev/fallback only; expires (~1h → 401).
  - `oauth_refresh` — OAuth2 with a stored refresh_token; refreshes the access_token near expiry and **writes the rotation back** to Key Vault (Claude/Codex-style, for user-connected servers). The OAuth callback (`router.py` `/mcp/connections/oauth/callback`) persists the full blob (`token_endpoint`, `client_id`, `client_secret`, `expires_at`, `scopes`); write-back needs KV write (the ca-mcp MI has Key Vault Administrator).
  - `managed_identity` — mints a **fresh AAD token per call** from the ca-mcp MI for `audience` (Azure Managed Grafana: `ce34e7e5-485f-4d76-964f-b3d2b16d1e4f/.default`); nothing stored. Operator path for Azure platform resources; the MI needs a role on the target (e.g. Grafana Viewer). A `managed_identity` source **WINS over any `access_token` the agent passes** — `access_token` is honored only for direct/manual (raw-URL) connections.
- `credential_source` is **derived** from `auth_type` on register (`_derive_credential_source` validator: oauth2→oauth_refresh, api_key/bearer→static_secret), so the App UI needs no new fields — `managed_identity` is set explicitly by operator `PUT /mcp/connections/servers/{id}`. NOT exposed as a UI auth type (MI is not an MCP-client concept).

### Persistence & telemetry
- Cosmos DB throughout: chat history (`common/services/chat_cosmos_service.py`), plans, MCP connections. `config.COSMOSDB_DATABASE` + per-purpose containers.
- App Insights / OpenTelemetry is wired in `app.py` (`configure_azure_monitor` + `FastAPIInstrumentor`); the Azure AI SDK auto-instruments the `/responses` calls. **Exceptions, dependency failures (status codes, request IDs, correlation) are already captured in Log Analytics** — don't add bespoke error-capture code for observability; query `exceptions` / `dependencies` instead.

## Gotchas learned the hard way

- **`agent.invoke` failures are usually wrapped.** `agent_framework` raises `ChatClientException(...) from ex` — the useful typed exception (e.g. `openai.APIError` with `.code`/`.type`, `azure` `HttpResponseError` with `.status_code`) is reachable only via the `__cause__` chain, not the stringified message. Inspect the object/chain, never parse the message text.
- A transient AOAI `500` is `openai.APIError` (base) — it has **no `status_code`**, only `.code`/`.type` (e.g. `"server_error"`). Only `APIStatusError` subclasses carry `status_code`.
- The frontend silently full-reloads to `/` in local dev when `reauthSilently()` (`src/frontend/src/api/config.tsx`) hits a missing EasyAuth (`/.auth/login/aad`) endpoint — guard EasyAuth redirects to non-localhost only.
- Foundry agent definitions persist server-side; local instructions changes in `data/agent_teams/*.json` won't take effect until republished (`MACAE_FORCE_AGENT_PUBLISH=1`).
- **`aiohttp` must be a `src/mcp_server` dependency.** `azure.*.aio` (the `ManagedIdentityCredential` token mint AND Key Vault reads in `credential_resolver`) uses `aiohttp` as its async transport; if it's absent azure-core raises `"aiohttp package is not installed"`, the token is never obtained, and the upstream request goes out with **no `Authorization` header → 401** (silent — the failure is only in the ca-mcp log, not the HTTP error).
- **`update_mcp_server` must apply body via `existing.model_copy(update=...)`, not a `setattr` loop.** Pydantic v2 does not coerce on plain `setattr` (no `validate_assignment`), so a str like `"managed_identity"` set onto an enum field serializes back as `None` in `model_dump` — the update silently drops `credential_source`/`audience`. `model_copy(update=...)` runs it through model construction and keeps the enum. (It does NOT re-run validators, so it won't re-derive `credential_source` on an `auth_type`-only edit — fine for explicit PUTs.)

## CI/CD (GitHub Actions, desde 2026-08)

- **Flujo**: push a `stable/v4-baseline` → PR a `main` → required checks
  (`test`, `Backend (ruff + mypy)`, `MCP server (ruff)`, `Frontend (ESLint + build)`;
  `main` protegida) → **squash-merge** → `cd.yml` construye SOLO los componentes
  cambiados, tag `sha-<commit>`, y actualiza las apps EXISTENTES (backend/mcp:
  `az containerapp update`; frontend: `az webapp config container set`).
  Palanca manual: workflow_dispatch de CD con checkboxes por componente.
  **Los deploys jamás crean recursos** (nada de azd provision — incidente de
  costos histórico). Identidad: SP `copiloto-cli-sp` por OIDC federado.
- **Ritual post-squash-merge** (obligatorio antes del siguiente commit):
  `git fetch origin && git reset --hard origin/main && git push --force-with-lease origin stable/v4-baseline`.
  Sin esto la divergencia de historia renace en cada ciclo (los commits de la
  rama nunca entran a la historia de main con squash).
- **Dependabot**: mensual agrupado; ecosistema `uv` para backend/mcp (NUNCA
  `pip`: editaría requirements.txt sin tocar uv.lock), `npm` frontend,
  `github-actions`. Etiqueta `auto-merge` + workflow que arma el auto-merge
  nativo con guardia fail-closed (aborta si main no tiene required checks).
  Merges hechos por GITHUB_TOKEN NO disparan cd.yml (guard anti-bucles de
  GitHub) — esas dependencias llegan a prod con el siguiente merge humano.
- Workflows quality-gate y test corren sin filtro de paths en PR: un required
  check cuyo workflow no corre deja el PR colgado en "Expected" para siempre.

## Stale references to ignore (pre-Agent-Framework)

Anything mentioning "Semantic Kernel"/"Planner Agent" (it's Agent Framework
Magentic), `app_kernel:app` (it's `app:app`), or MCP on `:8001` (it's
`MCP_SERVER_ENDPOINT`, dev `:9000`) predates the migration — treat as stale.
(`.github/copilot-instructions.md` fue actualizado al mundo v4 y ya ES
confiable; `QUICK_START_LOCAL.md` ya no existe.)
