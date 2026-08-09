# GitHub Copilot Instructions — MACAE (Multi-Agent Custom Automation Engine)

Active backend is the **v4** stack on Microsoft **Agent Framework** (`agent_framework` +
`agent_framework_orchestrations`) driving **Azure AI Foundry** agents. Ignore any
reference to Semantic Kernel, `app_kernel:app`, a "Planner Agent", or MCP on `:8001` —
that is pre-migration and no longer true.

## Architecture Overview

Three runnable components:

```
Frontend (React 18 + Vite + FluentUI v9 + Redux, :3001)
  ├─ SSE      ──► Backend POST /api/v4/chat/message/stream   (direct chat)
  └─ WebSocket ─► Backend (FastAPI :8000, /api/v4/*)         (plan progress)
                     ├─ Agent Framework Magentic ──► Azure AI Foundry agents
                     ├─ MCP (streamable-http) ─────► MCP Server (FastMCP, $MCP_SERVER_ENDPOINT, dev :9000)
                     └─ Azure Cosmos DB ───────────► chat history / plans / mcp_connections
```

- **Backend** (`src/backend/`): FastAPI `app:app` (`app.py`). All v4 routes are in one file:
  `v4/api/router.py`. `OrchestrationManager` (`v4/orchestration/`) runs the Magentic
  group-chat workflow.
- **MCP Server** (`src/mcp_server/mcp_server.py`): standalone FastMCP server (HR, Marketing,
  Product, TechSupport, …) consumed by backend agents.
- **Frontend** (`src/frontend/`): services in `src/services/` are plain TS singletons (no hooks);
  `WebSocketService` manages one WS with per-plan subscriptions.

## Two request paths (know which one you are in)

1. **Direct conversational chat** — `POST /api/v4/chat/message/stream` (SSE). Intent-classified
   (`v4/orchestration/intent_router.py`); builds a throwaway workflow via
   `_create_direct_response_workflow`, streams through `agent.invoke`, and **closes those agents
   when the request ends**.
2. **Formal plan / task** — `POST /api/v4/process_request` (+ `init_team`, `resume_plan`,
   `plan_approval`). Builds/loads a **session-scoped** orchestration
   (`OrchestrationManager.get_current_or_new_orchestration`) and runs it as a **FastAPI
   BackgroundTask** (HTTP returns immediately; work continues after). Progress streams over
   **WebSocket** (`WebsocketMessageType` events), not SSE.

## Developer Workflows

```bash
# Backend (uv: pyproject.toml + uv.lock)
cd src/backend && uv run uvicorn app:app --port 8000 --reload
uv run pytest -m "not integration"                      # skip live-Azure tests
uv run pytest src/tests/backend/test_x.py::test_y -v    # single test
ruff check . && ruff format .                           # line-length 88, py3.11

# Frontend
cd src/frontend && npm run dev        # :3001
npm run build                          # tsc && vite build
npm run lint                           # eslint src --ext .js,.jsx,.ts,.tsx
npm run test                           # vitest

# MCP server (HTTP transport; backend points at it via MCP_SERVER_ENDPOINT)
cd src/mcp_server && uv run python mcp_server.py --transport streamable-http --port 9000
```

Tests live in `src/tests/backend` and `src/tests/agents` (root `pytest.ini`); markers:
`unit`, `integration` (live Azure), `e2e` (Playwright, in `tests/e2e-test/`).

## Configuration & auth

- One config singleton: `common/config/app_config.py` → `config`. `config.APP_ENV`
  (`"dev"`|`"prod"`) gates credentials: `prod` = Managed Identity; `dev` =
  `DefaultAzureCredentialAsync(exclude_environment_credential=True)` → your `az login`
  (**SP env vars are ignored in dev** due to that exclusion).
- Foundry data-plane audience is `https://ai.azure.com`; requires the **Azure AI Developer**
  role. A guest/external (`#EXT#`) identity is rejected even with the right audience.
- **OBO**: `config.build_user_credential(token)` → real `OnBehalfOfCredential` when `ENABLE_OBO`
  is set, else `StaticTokenCredential` (forwards the EasyAuth token verbatim — wrong audience for
  Foundry in local dev).
- Process-scoped shared `credential`/`AIProjectClient` are created once and closed once in
  `app.py`'s `lifespan` (`config.aclose_shared_resources()`). Agents **borrow** them — never enter
  them into a per-agent `AsyncExitStack` nor close them.
- Endpoints never read headers directly: use `_extract_auth(request) -> (user_id, tenant_id)`,
  or `_extract_auth_with_token(...) -> (user_id, tenant_id, access_token)` for OBO.

## Conventions & patterns

- **Agents**: `MagenticAgentFactory.get_agents` builds one `FoundryAgentTemplate` per team-config
  entry. Teams are JSON in `data/agent_teams/*.json` (`TeamConfiguration`), persisted via
  `TeamService`. Flags: `use_rag` (Azure AI Search), `use_mcp`, `coding_tools` (code interpreter),
  `use_reasoning` (switches to `REASONING_MODEL_NAME`).
- **Client selection** (`foundry_agent.py`): `AzureAIClient` for a published Foundry agent
  (`use_latest_version=True`, server-side tools, no runtime tools); `AzureOpenAIResponsesClient`
  when runtime MCP tools are attached. Foundry agent definitions are **reused by name**
  (`_FOUNDRY_REGISTERED_AGENT_NAMES`); set `MACAE_FORCE_AGENT_PUBLISH=1` to republish.
- **OrchestrationManager**: the Magentic **workflow is single-use** (rebuilt per task); **agents
  are reused** across tasks/approvals of the same team and torn down only on team switch / shutdown.
- **MCP — two disconnected mechanisms**: agents call the single `MCPConfig.from_env()` server
  (`MCP_SERVER_ENDPOINT`). The UI "Aplicaciones" connector registry (`/mcp/connections/...`,
  `MCPConnectionsService` → Cosmos `mcp_connections`, secrets in Key Vault) is **not consumed by
  the agent runtime** — connecting a server in the UI does not make agents able to call it. Foundry
  portal servers reach agents only because they are bound server-side to the published agent.
- **Telemetry**: `configure_azure_monitor` + `FastAPIInstrumentor` (`app.py`) plus the Azure AI SDK
  auto-instrument `/responses`. Exceptions/dependency failures (status, request id, correlation)
  are already in Log Analytics — query `exceptions`/`dependencies`, don't add bespoke capture code.

## Gotchas

- `agent.invoke` errors are wrapped: `agent_framework` raises `ChatClientException(...) from ex`.
  The typed cause (`openai.APIError`, `azure HttpResponseError`) is reachable only via the
  `__cause__` chain — inspect the object, never the stringified message.
- A transient AOAI 500 is base `openai.APIError` (**no `status_code`**, only `.code`/`.type` e.g.
  `"server_error"`); only `APIStatusError` subclasses carry `status_code`.
- Foundry agent instructions are server-side: edits to `data/agent_teams/*.json` need
  `MACAE_FORCE_AGENT_PUBLISH=1` to take effect.
