"""
MCP Connections Service — Cosmos DB-backed registry of external MCP servers.

Responsibilities:
- CRUD for MCPServerEntry (catalog — what servers are available)
- CRUD for MCPUserConnection (per-user — which servers each user has linked)
- Integration with CredentialResolver for token storage/retrieval
- Auto-discovery: agents query this service to find servers by capability

Does NOT store tokens — delegates to CredentialResolver (Key Vault).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from azure.cosmos.aio import CosmosClient

from common.config.app_config import config
from v4.common.models.mcp_connection_models import (
    MCPAuthType,
    MCPConnectionStatus,
    MCPServerEntry,
    MCPUserConnection,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Partition-key helpers
# ---------------------------------------------------------------------------


def _catalog_pk(tenant_id: str = "") -> str:
    """Compute the catalog partition key for a given tenant.

    - Shared / no tenant: ``"catalog"``
    - Tenant-scoped:      ``"catalog#<tenant_id>"``
    """
    return f"catalog#{tenant_id}" if tenant_id else "catalog"


def _user_pk(user_id: str, tenant_id: str = "") -> str:
    """Compute the user-connection partition key.

    - Legacy / single-tenant:  ``"<user_id>"``
    - Multi-tenant:             ``"<tenant_id>#<user_id>"``
    """
    return f"{tenant_id}#{user_id}" if tenant_id else user_id


def _get_container_name() -> str:
    """Get the MCP connections container name from config or default."""
    return (
        getattr(config, "COSMOSDB_MCP_CONNECTIONS_CONTAINER", None) or "mcp_connections"
    )


class MCPConnectionsService:
    """Manages the MCP server catalog and per-user connection index in Cosmos DB."""

    _instance: Optional["MCPConnectionsService"] = None

    def __init__(self):
        self._client: Optional[CosmosClient] = None
        self._container = None
        self._initialized = False

    @staticmethod
    async def get_instance() -> "MCPConnectionsService":
        """Singleton accessor — initializes on first call."""
        if MCPConnectionsService._instance is None:
            svc = MCPConnectionsService()
            await svc._ensure_initialized()
            MCPConnectionsService._instance = svc
        return MCPConnectionsService._instance

    async def _ensure_initialized(self) -> None:
        """Lazy-init Cosmos client and container reference."""
        if self._initialized:
            return

        try:
            endpoint = config.COSMOSDB_ENDPOINT
            if not endpoint:
                raise ValueError("COSMOSDB_ENDPOINT not configured")

            container_name = _get_container_name()
            credential = config.get_azure_credentials()
            self._client = CosmosClient(url=endpoint, credential=credential)
            db = self._client.get_database_client(config.COSMOSDB_DATABASE)

            # Get or create the mcp_connections container
            # In production this is created by Bicep; locally we create if missing
            try:
                self._container = db.get_container_client(container_name)
                # Verify it exists with a metadata read
                await self._container.read()
            except Exception:
                logger.info(
                    "Container '%s' not found — creating with /pk partition key",
                    container_name,
                )
                db_proxy = self._client.get_database_client(config.COSMOSDB_DATABASE)
                self._container = await db_proxy.create_container_if_not_exists(
                    id=container_name,
                    partition_key={"paths": ["/pk"], "kind": "Hash", "version": 2},
                    default_ttl=-1,  # enable TTL but don't auto-expire by default
                )

            self._initialized = True
            logger.info(
                "MCPConnectionsService initialized (container=%s)", container_name
            )

        except Exception as e:
            logger.error("Failed to initialize MCPConnectionsService: %s", e)
            raise

    # =========================================================================
    # Server Catalog (MCPServerEntry) — pk = "catalog"
    # =========================================================================

    async def list_servers(
        self, enabled_only: bool = True, tenant_id: str = ""
    ) -> List[MCPServerEntry]:
        """List all servers in the catalog for a tenant (or global shared catalog)."""
        await self._ensure_initialized()

        where = "AND c.enabled = true" if enabled_only else ""
        query = f"SELECT * FROM c WHERE c.doc_type = 'mcp_server' {where} ORDER BY c.display_name"

        items = []
        async for item in self._container.query_items(
            query=query,
            partition_key=_catalog_pk(tenant_id),
        ):
            try:
                items.append(MCPServerEntry.model_validate(item))
            except Exception as e:
                logger.warning("Invalid server entry %s: %s", item.get("id"), e)
        return items

    async def get_server(self, server_id: str, tenant_id: str = "") -> Optional[MCPServerEntry]:
        """Get a server entry by ID."""
        await self._ensure_initialized()
        try:
            item = await self._container.read_item(
                item=server_id, partition_key=_catalog_pk(tenant_id)
            )
            return MCPServerEntry.model_validate(item)
        except Exception:
            return None

    async def get_server_by_name(
        self, server_name: str, tenant_id: str = ""
    ) -> Optional[MCPServerEntry]:
        """Find a server by its unique server_name within a tenant catalog."""
        await self._ensure_initialized()

        query = (
            "SELECT * FROM c WHERE c.doc_type = 'mcp_server' AND c.server_name = @name"
        )
        params = [{"name": "@name", "value": server_name}]

        async for item in self._container.query_items(
            query=query,
            parameters=params,
            partition_key=_catalog_pk(tenant_id),
        ):
            return MCPServerEntry.model_validate(item)
        return None

    async def upsert_server(self, entry: MCPServerEntry) -> MCPServerEntry:
        """Create or update a server catalog entry.

        The partition key is derived from entry.tenant_id automatically.
        """
        await self._ensure_initialized()

        entry.pk = _catalog_pk(entry.tenant_id or "")
        entry.doc_type = "mcp_server"
        entry.updated_at = datetime.now(timezone.utc)

        doc = entry.model_dump(mode="json")
        # Ensure datetimes are ISO strings
        for key in ("created_at", "updated_at"):
            if isinstance(doc.get(key), datetime):
                doc[key] = doc[key].isoformat()

        await self._container.upsert_item(body=doc)
        logger.info("Upserted MCP server: %s (%s)", entry.server_name, entry.id)
        return entry

    async def delete_server(self, server_id: str, tenant_id: str = "") -> bool:
        """Remove a server from the catalog."""
        await self._ensure_initialized()
        try:
            await self._container.delete_item(
                item=server_id, partition_key=_catalog_pk(tenant_id)
            )
            logger.info("Deleted MCP server: %s", server_id)
            return True
        except Exception as e:
            logger.warning("Failed to delete server %s: %s", server_id, e)
            return False

    async def find_servers_for_agent(
        self, agent_type: str, tenant_id: str = ""
    ) -> List[MCPServerEntry]:
        """Find enabled servers that a specific agent type is allowed to use."""
        await self._ensure_initialized()

        # Servers with empty allowed_agents = available to all agents
        query = (
            "SELECT * FROM c WHERE c.doc_type = 'mcp_server' "
            "AND c.enabled = true "
            "AND (ARRAY_LENGTH(c.allowed_agents) = 0 OR ARRAY_CONTAINS(c.allowed_agents, @agent))"
        )
        params = [{"name": "@agent", "value": agent_type}]

        items = []
        async for item in self._container.query_items(
            query=query,
            parameters=params,
            partition_key=_catalog_pk(tenant_id),
        ):
            try:
                items.append(MCPServerEntry.model_validate(item))
            except Exception as e:
                logger.warning("Invalid server entry: %s", e)
        return items

    # =========================================================================
    # User Connections (MCPUserConnection) — pk = user_id
    # =========================================================================

    async def get_user_connections(
        self, user_id: str, active_only: bool = True, tenant_id: str = ""
    ) -> List[MCPUserConnection]:
        """Get all MCP server connections for a user (optionally tenant-scoped)."""
        await self._ensure_initialized()

        where = "AND c.status = 'active'" if active_only else ""
        query = f"SELECT * FROM c WHERE c.doc_type = 'mcp_user_connection' {where}"

        items = []
        async for item in self._container.query_items(
            query=query,
            partition_key=_user_pk(user_id, tenant_id),
        ):
            try:
                items.append(MCPUserConnection.model_validate(item))
            except Exception as e:
                logger.warning("Invalid user connection: %s", e)
        return items

    async def get_user_connection(
        self, user_id: str, server_name: str, tenant_id: str = ""
    ) -> Optional[MCPUserConnection]:
        """Get a specific user connection by server name."""
        await self._ensure_initialized()

        query = (
            "SELECT * FROM c WHERE c.doc_type = 'mcp_user_connection' "
            "AND c.server_name = @server_name"
        )
        params = [{"name": "@server_name", "value": server_name}]

        async for item in self._container.query_items(
            query=query,
            parameters=params,
            partition_key=_user_pk(user_id, tenant_id),
        ):
            return MCPUserConnection.model_validate(item)
        return None

    async def upsert_user_connection(
        self, connection: MCPUserConnection
    ) -> MCPUserConnection:
        """Create or update a user connection.

        The partition key is derived from connection.tenant_id and
        connection.user_id automatically.
        """
        await self._ensure_initialized()

        connection.pk = _user_pk(connection.user_id, connection.tenant_id)
        connection.doc_type = "mcp_user_connection"

        doc = connection.model_dump(mode="json")
        for key in ("connected_at", "last_used_at", "token_expires_at"):
            val = doc.get(key)
            if isinstance(val, datetime):
                doc[key] = val.isoformat()

        await self._container.upsert_item(body=doc)
        logger.info(
            "Upserted user connection: tenant=%s user=%s server=%s status=%s",
            connection.tenant_id[:8] if connection.tenant_id else "",
            connection.user_id,
            connection.server_name,
            connection.status,
        )
        return connection

    async def mark_connection_active(
        self,
        user_id: str,
        server_name: str,
        secret_ref: Optional[str] = None,
        tenant_id: str = "",
    ) -> MCPUserConnection:
        """Mark a user connection as active (after successful auth)."""
        conn = await self.get_user_connection(user_id, server_name, tenant_id=tenant_id)
        if not conn:
            raise ValueError(
                f"No connection found for user={user_id} server={server_name}"
            )

        conn.status = MCPConnectionStatus.ACTIVE
        conn.last_used_at = datetime.now(timezone.utc)
        if secret_ref:
            conn.secret_ref = secret_ref
        return await self.upsert_user_connection(conn)

    async def touch_connection(
        self, user_id: str, server_name: str, tenant_id: str = ""
    ) -> None:
        """Update last_used_at to keep TTL alive."""
        conn = await self.get_user_connection(user_id, server_name, tenant_id=tenant_id)
        if conn:
            conn.last_used_at = datetime.now(timezone.utc)
            await self.upsert_user_connection(conn)

    async def disconnect_user(
        self, user_id: str, server_name: str, tenant_id: str = ""
    ) -> bool:
        """Remove a user's connection to a server."""
        await self._ensure_initialized()

        conn = await self.get_user_connection(user_id, server_name, tenant_id=tenant_id)
        if not conn:
            return False

        try:
            await self._container.delete_item(
                item=conn.id, partition_key=_user_pk(user_id, tenant_id)
            )
            logger.info("Disconnected user=%s from server=%s", user_id, server_name)
            return True
        except Exception as e:
            logger.warning("Failed to disconnect: %s", e)
            return False

    # =========================================================================
    # Combined queries (for agent auto-connect)
    # =========================================================================

    async def get_available_servers_for_user(
        self, user_id: str, agent_type: str = "", tenant_id: str = ""
    ) -> List[Dict[str, Any]]:
        """
        Get all available servers with user's connection status.

        Returns merged view: server catalog + user connection status.
        Used by agents to decide which servers to auto-connect.
        """
        # Get catalog (tenant-scoped when tenant_id is set)
        if agent_type:
            servers = await self.find_servers_for_agent(agent_type, tenant_id=tenant_id)
        else:
            servers = await self.list_servers(enabled_only=True, tenant_id=tenant_id)

        # Get user's existing connections (tenant-scoped)
        user_conns = await self.get_user_connections(user_id, active_only=False, tenant_id=tenant_id)
        conn_by_name = {c.server_name: c for c in user_conns}

        result = []
        for server in servers:
            conn = conn_by_name.get(server.server_name)

            # is_connected: only if connection exists AND is active
            is_connected = (
                conn is not None and conn.status == MCPConnectionStatus.ACTIVE
            )

            # needs_auth: server requires auth AND user doesn't have an active connection
            if server.auth_type == MCPAuthType.NONE:
                needs_auth = False
            elif conn is None:
                needs_auth = True
            else:
                # Connection exists but may be in a non-usable state
                needs_auth = conn.status in (
                    MCPConnectionStatus.PENDING_AUTH,
                    MCPConnectionStatus.EXPIRED,
                    MCPConnectionStatus.REVOKED,
                    MCPConnectionStatus.ERROR,
                )

            result.append(
                {
                    "server": server.model_dump(mode="json"),
                    "connection": conn.model_dump(mode="json") if conn else None,
                    "is_connected": is_connected,
                    "needs_auth": needs_auth,
                }
            )
        return result

    async def close(self) -> None:
        """Close the Cosmos client."""
        if self._client:
            await self._client.close()
            self._initialized = False
            logger.info("MCPConnectionsService closed")
