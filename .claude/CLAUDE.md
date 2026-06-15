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

# Tests (pytest.ini at repo root; testpaths = src/tests/backend, src/tests/agents)
uv run pytest                                   # all
uv run pytest -m "not integration"              # skip tests needing live Azure
uv run pytest src/tests/backend/test_x.py::test_y -v   # single test
# markers: unit, integration (live Azure), e2e (Playwright, needs running frontend)

# Lint/format (backend): ruff, line-length 88, Python 3.11
cd src/backend && ruff check . && ruff format .

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

### MCP — two disconnected mechanisms (important)
- **Agent runtime MCP** comes from `MCPConfig.from_env()` → the single `MCP_SERVER_ENDPOINT` server. That is the only MCP an agent built by the factory will call.
- **The UI "Aplicaciones" connector registry** (`POST /mcp/connections/...`, `MCPConnectionsService`) persists per-user connections to the Cosmos `mcp_connections` container (secrets in Key Vault via `credential_resolver.py`). **The agent runtime does NOT consume this registry** — registering/connecting a server in the UI does not make any agent able to call it. Servers attached in the **Foundry portal** reach agents only because they're bound server-side to the published Foundry agent.

### Persistence & telemetry
- Cosmos DB throughout: chat history (`common/services/chat_cosmos_service.py`), plans, MCP connections. `config.COSMOSDB_DATABASE` + per-purpose containers.
- App Insights / OpenTelemetry is wired in `app.py` (`configure_azure_monitor` + `FastAPIInstrumentor`); the Azure AI SDK auto-instruments the `/responses` calls. **Exceptions, dependency failures (status codes, request IDs, correlation) are already captured in Log Analytics** — don't add bespoke error-capture code for observability; query `exceptions` / `dependencies` instead.

## Gotchas learned the hard way

- **`agent.invoke` failures are usually wrapped.** `agent_framework` raises `ChatClientException(...) from ex` — the useful typed exception (e.g. `openai.APIError` with `.code`/`.type`, `azure` `HttpResponseError` with `.status_code`) is reachable only via the `__cause__` chain, not the stringified message. Inspect the object/chain, never parse the message text.
- A transient AOAI `500` is `openai.APIError` (base) — it has **no `status_code`**, only `.code`/`.type` (e.g. `"server_error"`). Only `APIStatusError` subclasses carry `status_code`.
- The frontend silently full-reloads to `/` in local dev when `reauthSilently()` (`src/frontend/src/api/config.tsx`) hits a missing EasyAuth (`/.auth/login/aad`) endpoint — guard EasyAuth redirects to non-localhost only.
- Foundry agent definitions persist server-side; local instructions changes in `data/agent_teams/*.json` won't take effect until republished (`MACAE_FORCE_AGENT_PUBLISH=1`).

## Stale references to ignore (pre-Agent-Framework)

`QUICK_START_LOCAL.md` and `.github/copilot-instructions.md` predate the Agent Framework migration.  `app:app`), "Semantic Kernel"/"Planner Agent" (it's Agent Framework Magentic), and MCP on `:8001` (it's `MCP_SERVER_ENDPOINT`, dev `:9000`).
