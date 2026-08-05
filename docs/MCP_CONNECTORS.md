# MCP Connectors — registry & credential flow

How MACAE agents reach **external** MCP servers (Azure Managed Grafana, GitHub, Vercel, …), and how each connection is authenticated.

> Scope: the **connector registry** and its credential model. This is separate from the agent's own runtime MCP endpoint (`MCP_SERVER_ENDPOINT` → the MacaeMcpServer / `ca-mcp` container), which is always present.

---

## The big picture

An agent never talks to an external MCP server directly. It calls **MacaeMcpServer** (the `ca-mcp` container) tools, which look the server up in a shared catalog and connect on the agent's behalf:

```
agent ──▶ MacaeMcpServer tool ──▶ catalog lookup ──▶ resolve a valid token ──▶ external MCP server
          (connect_from_registry,   (Cosmos            (credential_resolver,      (Grafana, GitHub…)
           connect_mcp_server,        mcp_connections)    by credential_source)
           call_external_tool,
           discover_mcp_capabilities)
```

- **Catalog** (`MCPServerEntry`, Cosmos `mcp_connections`, pk=`catalog`): the list of servers agents *can* connect to. Populated by the App **"Aplicaciones"** settings UI (`POST /mcp/connections/servers`) or by an operator `PUT`.
- **Per-user connections** (`MCPUserConnection`, pk=user): which servers a user has authorized, plus a `secret_ref` pointing at Key Vault (never the token itself).
- **MacaeMcpServer tools** (`src/mcp_server/services/inspector_service.py`): `connect_from_registry`, `connect_mcp_server`, `discover_mcp_capabilities`, `call_external_tool`.

Registering a server in the UI **does** make it usable by agents — through these tools. (Servers bound in the **Foundry portal** are a different, parallel path: they reach agents because they're attached server-side to the published Foundry agent.)

---

## `auth_type` vs `credential_source`

Two independent fields on the catalog entry — do not conflate them:

| Field | Means | Typical value |
|-------|-------|---------------|
| **`auth_type`** | What the target server sees on the wire | `bearer_token` (almost always — standard MCP) |
| **`credential_source`** | How *we* obtain a valid token | `static_secret` / `oauth_refresh` / `managed_identity` |

The target server only ever sees a **Bearer token**. *Where that token comes from* is `credential_source`, resolved by one dispatcher:

`credential_resolver.resolve_valid_token(credential_source, audience, secret_ref)` — `src/mcp_server/credential_resolver.py`.

### The three sources

| `credential_source` | Behavior | Stored in Key Vault? | Use for |
|---------------------|----------|----------------------|---------|
| **`static_secret`** | Returns a fixed token as-is. Expires (~1h → 401). | Yes (the token) | Dev / fallback only |
| **`oauth_refresh`** | Uses the stored `access_token`; when near expiry, exchanges the `refresh_token` at `token_endpoint` for a new one and **writes the rotation back** to Key Vault. | Yes (full blob) | User-connected servers (GitHub, etc.) — Claude/Codex-style |
| **`managed_identity`** | Mints a **fresh AAD token per call** from the ca-mcp Managed Identity for `audience`. Nothing stored, never expires on us. Wins over any caller-passed token. | No | Azure platform resources (Grafana) — operator-configured |

---

## Registering a server

### User-connected servers (the normal case — through the App UI)

Users add a server in **Settings → Aplicaciones**, pick an `auth_type` (OAuth 2.0 / API Key / Bearer), and authorize. **No infra knowledge required** — `credential_source` is derived automatically:

| UI `auth_type` | Derived `credential_source` |
|----------------|-----------------------------|
| OAuth 2.0 | `oauth_refresh` (durable — refreshes automatically) |
| API Key / Bearer | `static_secret` |

(Derivation: `_derive_credential_source` validator on `MCPServerEntry`.) `managed_identity` is intentionally **not** a UI option — it's not an MCP-client concept, and no mainstream MCP client exposes it.

### Platform / Azure-resource servers (operator only — via `PUT`)

For an Azure resource whose MCP endpoint accepts an AAD token (e.g. Azure Managed Grafana), register it as `managed_identity` explicitly — the ca-mcp MI mints the token, so nothing is stored and nothing expires:

```bash
# 1. Find the server_id
curl -s http://localhost:8000/api/v4/mcp/connections/servers \
  | python3 -c "import sys,json;[print(s['id'],s['server_name'],s.get('auth_type'),s.get('credential_source'),s.get('audience')) for s in json.load(sys.stdin)['servers']]"

# 2. Set credential_source + audience
curl -X PUT http://localhost:8000/api/v4/mcp/connections/servers/<SERVER_ID> \
  -H "Content-Type: application/json" \
  -d '{"auth_type":"bearer_token","credential_source":"managed_identity","audience":"<AAD-audience>/.default"}'
```

The operator PUT with an explicit `credential_source` is always respected (the derivation only runs when it's absent).

---

## Worked example — Azure Managed Grafana

`azuremanagedgrafana` catalog entry:

```json
{
  "auth_type": "bearer_token",
  "credential_source": "managed_identity",
  "audience": "ce34e7e5-485f-4d76-964f-b3d2b16d1e4f/.default"
}
```

`ce34e7e5-485f-4d76-964f-b3d2b16d1e4f` is the Azure Managed Grafana first-party AAD app; its `/api/azure-mcp` endpoint accepts an AAD token for that audience.

Prerequisites (one-time, operator):
- The **ca-mcp Managed Identity** holds a role on the Grafana instance — **Grafana Viewer** (or higher).
- `aiohttp` is a `src/mcp_server` dependency (see below).

Then any agent can:
```
connect_from_registry(server_name="azuremanagedgrafana")   # → active, no token passed
discover_mcp_capabilities(server_name="azuremanagedgrafana") # → 25 amgmcp_* tools
call_external_tool(server_name="azuremanagedgrafana", target_tool="amgmcp_kusto_query", arguments={...})
```

---

## Prerequisites & environment

**ca-mcp Managed Identity roles:**
- `Key Vault Secrets User` — read secrets (`static_secret`, `oauth_refresh` read).
- Key Vault write (`Secrets Officer` or `Administrator`) — required for `oauth_refresh` **write-back**.
- A role on each `managed_identity` target resource (e.g. Grafana Viewer).

**ca-mcp environment:** `AZURE_KEY_VAULT_URL`, `CLIENT_ID` (the MI client id — the resolver reads `AZURE_CLIENT_ID` **or** `CLIENT_ID`), `MACAE_BACKEND_URL` (so the registry bridge reaches the backend, not localhost).

**Dependency:** `aiohttp` **must** be in `src/mcp_server/pyproject.toml`. `azure.*.aio` (the MI token mint and Key Vault reads) uses it as the async transport; without it the SDK raises `"aiohttp package is not installed"`.

---

## Troubleshooting a 401 from the target server

A 401 alone does not tell you *why*. Pull the ca-mcp logs and see which path ran:

```bash
az containerapp logs show -n ca-mcp-<suffix> -g <rg> --tail 200 --type console \
  | grep -iE "connect_mcp_server|managed-identity|aiohttp|401|Forwarding inbound"
```

| Log line | Meaning | Fix |
|----------|---------|-----|
| `credential_source resolution failed: aiohttp package is not installed` | MI mint / KV read couldn't run; request sent with **no** `Authorization` header | Add `aiohttp` to `src/mcp_server` deps, rebuild |
| `Forwarding inbound Authorization header` (for a `managed_identity` server) | A caller-passed token short-circuited the MI mint | Ensure `credential_source=managed_identity` is set; MI resolution now wins over `access_token` |
| `Resolved managed-identity token …` **then** 401 | Token minted but rejected | MI lacks a role on the target, or the `audience` is wrong |
| `static_secret` token 401 | Stored token expired | Re-auth (or switch to `oauth_refresh` / `managed_identity`) |

---

## Files

| Concern | File |
|---------|------|
| Catalog & connection models, `credential_source` enum, derivation validator | `src/backend/v4/common/models/mcp_connection_models.py` |
| Registry service (Cosmos) | `src/backend/v4/common/services/mcp_connections_service.py` |
| Register / update / connect / OAuth-callback endpoints | `src/backend/v4/api/router.py` (`/mcp/connections/...`) |
| Token dispatcher (MI mint, OAuth refresh + write-back, static) | `src/mcp_server/credential_resolver.py` |
| Connect tools consuming the registry | `src/mcp_server/services/inspector_service.py` |
