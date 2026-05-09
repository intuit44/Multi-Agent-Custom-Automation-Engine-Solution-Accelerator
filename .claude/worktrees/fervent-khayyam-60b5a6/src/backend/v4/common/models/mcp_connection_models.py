"""
MCP Connection Models — Cosmos DB index for external MCP server connections.

Two document types stored in the `mcp_connections` container:

1. MCPServerEntry (pk = "catalog")
   - Shared catalog of available MCP servers
   - Registered by admin, discovered by agents at runtime
   - NO secrets — only endpoint URLs and metadata

2. MCPUserConnection (pk = user_id)
   - Per-user connection status to each MCP server
   - Tracks which servers each user has active
   - Secret references point to Key Vault via CredentialResolver
   - NO tokens stored here — only `secret_ref` URIs

Credential flow:
  Agent needs MCP server → MCPConnectionsService.get_user_connections(user_id)
  → finds server entry → CredentialResolver.resolve_credentials(project_id, server_name)
  → Key Vault → token → InspectorService.connect_mcp_server(url, headers)
"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class MCPTransportType(str, Enum):
    """Supported MCP transport protocols."""

    STREAMABLE_HTTP = "streamable-http"
    SSE = "sse"
    STDIO = "stdio"


class MCPAuthType(str, Enum):
    """How the MCP server authenticates clients."""

    NONE = "none"
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    BEARER_TOKEN = "bearer_token"
    MANAGED_IDENTITY = "managed_identity"


class MCPConnectionStatus(str, Enum):
    """User-level connection status to an MCP server."""

    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    PENDING_AUTH = "pending_auth"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Cosmos DB Documents
# ---------------------------------------------------------------------------


class MCPServerEntry(BaseModel):
    """
    Catalog entry for an available MCP server.

    Stored in Cosmos `mcp_connections` container with pk="catalog" (shared)
    or pk="catalog#{tenant_id}" (tenant-scoped).
    Represents a server that CAN be connected to — no secrets stored here.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    pk: str = "catalog"  # partition key — set by MCPConnectionsService before write
    doc_type: str = "mcp_server"

    # Multi-tenancy — empty string means globally shared catalog entry
    tenant_id: Optional[str] = Field(
        default=None,
        description="AAD tenant ID. None / empty = shared catalog visible to all tenants.",
    )

    # Identity
    server_name: str  # unique key, e.g. "github-corp", "slack-workspace"
    display_name: str  # UI-friendly name, e.g. "GitHub Corporate"
    description: str = ""
    icon_url: Optional[str] = None

    # Connection
    endpoint: str  # full URL, e.g. "https://mcp-github.corp.com/mcp"
    transport: MCPTransportType = MCPTransportType.STREAMABLE_HTTP

    # Auth metadata (NO secrets — just what type and what fields are needed)
    auth_type: MCPAuthType = MCPAuthType.NONE
    auth_fields: List[str] = Field(
        default_factory=list,
        description="Credential field names required, e.g. ['api_key'] or ['client_id','client_secret']",
    )
    oauth_scopes: List[str] = Field(
        default_factory=list,
        description="OAuth2 scopes if auth_type is oauth2",
    )

    # Capabilities discovered on last connect (cached)
    capabilities: List[str] = Field(
        default_factory=list,
        description="['tools', 'resources', 'prompts']",
    )
    tool_count: int = 0
    resource_count: int = 0

    # Access control
    allowed_agents: List[str] = Field(
        default_factory=list,
        description="Agent types allowed to use this server. Empty = all agents.",
    )
    enabled: bool = True

    # Audit
    added_by: Optional[str] = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("added_by", mode="before")
    @classmethod
    def _normalize_added_by(cls, value):
        """Accept legacy/null catalog rows and coerce them to an empty string."""
        return value or ""


class MCPUserConnection(BaseModel):
    """
    Per-user connection status to an MCP server.

    Stored in Cosmos `mcp_connections` container.
    Partition key (pk):
      - Legacy / single-tenant:  pk = user_id
      - Multi-tenant:             pk = "{tenant_id}#{user_id}"
    The pk value is computed by MCPConnectionsService before write.

    Tracks WHICH servers a user has active — NOT the tokens themselves.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    pk: str = ""  # computed and set by MCPConnectionsService before upsert
    doc_type: str = "mcp_user_connection"

    # Multi-tenancy — empty string = single-tenant / legacy mode
    tenant_id: str = Field(
        default="",
        description="AAD tenant ID. Empty = legacy single-tenant mode (pk = user_id).",
    )

    # Links
    user_id: str  # AAD object ID
    server_id: str  # references MCPServerEntry.id
    server_name: str  # denormalized for fast reads

    # Status
    status: MCPConnectionStatus = MCPConnectionStatus.PENDING_AUTH
    last_error: Optional[str] = None

    # Credential reference (points to Key Vault, NOT the actual token)
    secret_ref: Optional[str] = Field(
        default=None,
        description="Key Vault secret URI, e.g. 'https://kv.vault.azure.net/secrets/mcp-user-github-abc123'",
    )
    scopes_granted: List[str] = Field(default_factory=list)

    # Timestamps
    connected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_used_at: Optional[datetime] = None
    token_expires_at: Optional[datetime] = None

    # TTL (Cosmos auto-delete expired connections after 30 days of inactivity)
    ttl: int = Field(
        default=2592000,  # 30 days in seconds
        description="Cosmos DB TTL for auto-cleanup of stale connections",
    )
