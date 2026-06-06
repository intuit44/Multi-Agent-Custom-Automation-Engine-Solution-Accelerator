"""Models for agent configurations."""

import logging
from dataclasses import dataclass

from common.config.app_config import config

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class MCPConfig:
    """Configuration for connecting to an MCP server."""

    url: str = ""
    name: str = "MCP"
    description: str = ""
    tenant_id: str = ""
    client_id: str = ""

    @classmethod
    def from_env(cls, name: str = "") -> "MCPConfig":
        url = config.MCP_SERVER_ENDPOINT
        mcp_name = name or config.MCP_SERVER_NAME
        description = config.MCP_SERVER_DESCRIPTION
        tenant_id = config.AZURE_TENANT_ID
        client_id = config.AZURE_CLIENT_ID

        # Raise exception if any required environment variable is missing
        if not all([url, mcp_name, description, tenant_id, client_id]):
            raise ValueError(f"{cls.__name__} Missing required environment variables")

        return cls(
            url=url,
            name=mcp_name,
            description=description,
            tenant_id=tenant_id,
            client_id=client_id,
        )

    @classmethod
    def from_foundry_connection(cls, connection_name: str) -> "MCPConfig":
        """Resolve an MCP endpoint from a Foundry project connection by name.

        Looks up ``connection_name`` via the AIProjectClient and returns an
        MCPConfig whose ``url`` points to the connection's MCP target.

        Usage (one line per agent)::

            mcp_cfg = MCPConfig.from_foundry_connection("outlook")
            agent = FoundryAgentTemplate(..., mcp_config=mcp_cfg)

        Raises:
            ValueError: If the connection is not found or has no target URL.
        """
        project_client = config.get_ai_project_client()
        connections = project_client.connections
        try:
            conn = connections.get(name=connection_name)
        except Exception as exc:
            raise ValueError(
                f"Foundry connection '{connection_name}' not found: {exc}"
            ) from exc

        target: str = getattr(conn, "target", None) or ""
        if not target or target == "_":
            raise ValueError(
                f"Foundry connection '{connection_name}' has no valid target URL. "
                "Make sure the connector is enabled and OAuth is completed in the Foundry portal."
            )

        _log.info("Resolved Foundry connection '%s' → %s", connection_name, target)
        return cls(
            url=target,
            name=connection_name,
            description=f"Foundry connection: {connection_name}",
            tenant_id=config.AZURE_TENANT_ID or "",
            client_id=config.AZURE_CLIENT_ID or "",
        )


@dataclass(slots=True)
class SearchConfig:
    """Configuration for connecting to Azure AI Search."""

    connection_name: str | None = None
    endpoint: str | None = None
    index_name: str | None = None
    search_query_type: str = (
        "semantic"  # Options: "simple", "vector_simple", "vector", "semantic", "hybrid"
    )
    top_k: int = 5  # Number of results to return

    @classmethod
    def from_env(cls, index_name: str) -> "SearchConfig":
        connection_name = config.AZURE_AI_SEARCH_CONNECTION_NAME
        endpoint = config.AZURE_AI_SEARCH_ENDPOINT

        # Raise exception if any required environment variable is missing
        if not all([connection_name, index_name, endpoint]):
            raise ValueError(
                f"{cls.__name__} Missing required Azure Search environment variables"
            )

        return cls(
            connection_name=connection_name,
            endpoint=endpoint,
            index_name=index_name,
            search_query_type="semantic",  # Use semantic query type (vector + reranking)
            top_k=5,
        )
