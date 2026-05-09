from __future__ import annotations

import inspect
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Optional, cast

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.azure import AzureOpenAIResponsesClient
from agent_framework_azure_ai import AzureAIClient, AzureAIProjectAgentOptions
from azure.ai.agents.aio import AgentsClient
from azure.ai.projects.models import PromptAgentDefinition

from common.config.app_config import config
from common.database.database_base import DatabaseBase
from common.models.messages_af import TeamConfiguration
from common.utils.utils_agents import generate_assistant_id
from v4.common.services.team_service import TeamService
from v4.config.agent_registry import agent_registry
from v4.magentic_agents.models.agent_models import MCPConfig

# Cache Foundry registrations per process so ephemeral runtime agents do not
# recreate the same persisted agent on every request.
_FOUNDRY_REGISTERED_AGENT_NAMES: set[str] = set()


class MCPEnabledBase:
    """
    Base that owns an AsyncExitStack and (optionally) prepares an MCP tool
    for subclasses to attach to ChatOptions (agent_framework style).
    Subclasses must implement _after_open() and assign self._agent.
    """

    def __init__(
        self,
        mcp: MCPConfig | None = None,
        team_service: TeamService | None = None,
        team_config: TeamConfiguration | None = None,
        project_endpoint: str | None = None,
        memory_store: DatabaseBase | None = None,
        agent_name: str | None = None,
        agent_description: str | None = None,
        agent_instructions: str | None = None,
        model_deployment_name: str | None = None,
        project_client=None,
        user_access_token: str | None = None,
    ) -> None:
        self._stack: AsyncExitStack | None = None
        self.mcp_cfg: MCPConfig | None = mcp
        self.mcp_tool: MCPStreamableHTTPTool | None = None
        self._agent: Agent | None = None
        self.team_service: TeamService | None = team_service
        self.team_config: TeamConfiguration | None = team_config
        self.client: Optional[AgentsClient] = None
        self.project_endpoint = project_endpoint
        self.creds = None
        self.memory_store: Optional[DatabaseBase] = memory_store
        self.agent_name: str | None = agent_name
        self.agent_description: str | None = agent_description
        self.agent_instructions: str | None = agent_instructions
        self.model_deployment_name: str | None = model_deployment_name
        self.project_client = project_client
        self.user_access_token = user_access_token
        self.logger = logging.getLogger(__name__)

    async def open(self) -> "MCPEnabledBase":
        if self._stack is not None:
            return self
        self._stack = AsyncExitStack()

        # Always use MI/CLI credential for Foundry API calls.
        # user_access_token is only for MCP tool forwarding (agent365 OBO),
        # not for the AzureAIClient/ResponsesClient credential.
        self.creds = config.get_azure_credential_async(config.AZURE_CLIENT_ID)
        if self._stack:
            await self._stack.enter_async_context(self.creds)
        # Create AgentsClient
        if self.project_endpoint is None:
            raise ValueError("project_endpoint cannot be None")
        self.client = AgentsClient(
            endpoint=self.project_endpoint,
            credential=self.creds,
        )
        if self._stack:
            await self._stack.enter_async_context(self.client)
        # Prepare MCP
        await self._prepare_mcp_tool()

        # Let subclass build agent client
        await self._after_open()

        # Register agent (best effort)
        try:
            agent_registry.register_agent(self)
        except Exception as exc:
            # Best-effort registration; log and continue without failing open()
            self.logger.warning(
                "Failed to register agent %s in agent_registry: %s",
                type(self).__name__,
                exc,
                exc_info=True,
            )

        return self

    async def close(self) -> None:
        if self._stack is None:
            return
        try:
            # 1. Close the underlying AzureAIClient / ResponsesClient (main network resource)
            _agent_close = getattr(self._agent, "close", None) if self._agent else None
            if callable(_agent_close):
                try:
                    result = _agent_close()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    self.logger.warning(
                        "Error closing AzureAIClient for agent %s: %s",
                        self.agent_name,
                        exc,
                    )

            # 2. Close AgentsClient (aiohttp-based REST client for Foundry API calls)
            if self.client and hasattr(self.client, "close"):
                try:
                    await self.client.close()
                except Exception as exc:
                    self.logger.debug(
                        "AgentsClient close error (non-critical): %s", exc
                    )

            # 3. Release the AsyncExitStack (includes MCP tool context).
            # The MCP cancel scope was created in the task that called open().
            # If close() runs from a DIFFERENT task (e.g. force-rebuild), AnyIO
            # raises a cancel-scope violation.  We catch it — the important
            # network resources above were already released.
            if self._stack:
                try:
                    await self._stack.aclose()
                except Exception as exc:
                    self.logger.debug(
                        "Stack release notice for agent '%s' (resources already closed): %s",
                        self.agent_name,
                        exc,
                    )

            # 4. Close credential token cache
            if self.creds and hasattr(self.creds, "close"):
                try:
                    await self.creds.close()
                except Exception as exc:
                    self.logger.debug("Credential close error (non-critical): %s", exc)

        finally:
            # 5. Unregister from global registry (safe, non-blocking operation)
            try:
                agent_registry.unregister_agent(self)
            except Exception as exc:
                self.logger.debug(
                    "Agent unregister error (non-critical) for '%s': %s",
                    self.agent_name,
                    exc,
                )

            # Null all references so GC does not attempt a second close
            self._stack = None
            self.mcp_tool = None
            self._agent = None
            self.client = None
            self.creds = None

    # Context manager
    async def __aenter__(self) -> "MCPEnabledBase":
        return await self.open()

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: D401
        await self.close()

    # Delegate to underlying agent
    def __getattr__(self, name: str) -> Any:
        if self._agent is not None:
            return getattr(self._agent, name)
        raise AttributeError(f"{type(self).__name__} has no attribute '{name}'")

    async def _after_open(self) -> None:
        """Subclasses must build self._agent here."""
        raise NotImplementedError

    def get_chat_client(self) -> "AzureAIClient[AzureAIProjectAgentOptions]":
        """Return AzureAIClient for agents WITHOUT runtime tools (e.g. Azure Search path).

        Uses agent_name with use_latest_version=True to get the latest agent version.
        Agent reuse is handled automatically by the SDK via agent_name.

        WARNING: AzureAIClient does NOT support runtime tools (MCP, dynamic functions).
        For agents with runtime tools, use get_responses_client() instead.
        """
        if self._agent and self._agent.client:
            return cast("AzureAIClient[AzureAIProjectAgentOptions]", self._agent.client)
        chat_client = AzureAIClient(
            project_endpoint=self.project_endpoint,
            agent_name=self.agent_name,
            model_deployment_name=self.model_deployment_name,
            credential=self.creds,
            use_latest_version=True,
        )
        self.logger.info(
            "Created new AzureAIClient (agent_name=%s, use_latest_version=True)",
            self.agent_name,
        )
        return chat_client

    def get_responses_client(self) -> AzureOpenAIResponsesClient:
        """Return AzureOpenAIResponsesClient for agents WITH runtime tools.

        This client supports dynamic tools (MCP, functions) passed at runtime
        via Agent(tools=[...]).  Uses the Foundry project_endpoint so the
        execution goes through the same Azure AI project.

        Agents using this client are NOT automatically persisted in Foundry.
        Call _register_in_foundry() separately to make them visible in the
        Azure AI Foundry portal and extension.
        """
        responses_client = AzureOpenAIResponsesClient(
            project_endpoint=self.project_endpoint,
            deployment_name=self.model_deployment_name,
            credential=self.creds,
        )
        self.logger.info(
            "Created AzureOpenAIResponsesClient (deployment=%s, project_endpoint=%s)",
            self.model_deployment_name,
            self.project_endpoint,
        )
        return responses_client

    async def _register_in_foundry(self) -> None:
        """Persist agent definition in Azure AI Foundry via create_version.

        This ensures the agent is visible in the Foundry portal and VS Code
        extension, even when using AzureOpenAIResponsesClient for execution.
        Called from subclasses that use the responses client path.

        Behavior:
          - Default: if agent already exists in Foundry, reuse it (skip publish).
            This prevents version bloat on every backend restart.
          - With env var MACAE_FORCE_AGENT_PUBLISH=1: ALWAYS publish a new
            version with current local instructions. Use after editing
            data/agent_teams/*.json system_messages so changes propagate.
            Unset after one successful run to prevent accumulating versions.
        """
        if not self.project_client or not self.agent_name:
            return

        force_republish = os.getenv("MACAE_FORCE_AGENT_PUBLISH", "").lower() in (
            "1",
            "true",
            "yes",
        )

        if self.agent_name in _FOUNDRY_REGISTERED_AGENT_NAMES and not force_republish:
            self.logger.info(
                "Agent '%s' already registered in Foundry (cached for this process).",
                self.agent_name,
            )
            return

        try:
            existing_agent = None
            if not force_republish:
                async for agent in self.project_client.agents.list():
                    if getattr(agent, "name", None) == self.agent_name:
                        existing_agent = agent
                        break

            if existing_agent is not None and not force_republish:
                _FOUNDRY_REGISTERED_AGENT_NAMES.add(self.agent_name)
                _versions = getattr(existing_agent, "versions", None)
                _latest = (
                    getattr(_versions, "latest", None)
                    if _versions is not None
                    else None
                )
                _version_str = (
                    getattr(_latest, "version", "unknown")
                    if _latest is not None
                    else "unknown"
                )
                _version_id = (
                    getattr(_latest, "id", "unknown")
                    if _latest is not None
                    else "unknown"
                )
                self.logger.info(
                    "Agent '%s' already exists in Foundry (id=%s, latest_version=%s, version_id=%s); reusing it. "
                    "Set MACAE_FORCE_AGENT_PUBLISH=1 to republish with updated instructions.",
                    self.agent_name,
                    getattr(existing_agent, "id", "unknown"),
                    _version_str,
                    _version_id,
                )
                return

            if force_republish:
                self.logger.info(
                    "Force-publishing agent '%s' (MACAE_FORCE_AGENT_PUBLISH=1)",
                    self.agent_name,
                )

            agent_def = await self.project_client.agents.create_version(
                agent_name=self.agent_name,
                description=self.agent_description or "",
                definition=PromptAgentDefinition(
                    model=self.model_deployment_name or "",
                    instructions=self.agent_instructions or "",
                ),
            )
            self.logger.info(
                "Registered agent '%s' in Foundry (id=%s, version=%s)",
                self.agent_name,
                agent_def.id,
                agent_def.version,
            )
            _FOUNDRY_REGISTERED_AGENT_NAMES.add(self.agent_name)
        except Exception as exc:
            self.logger.warning(
                "Could not register agent '%s' in Foundry: %s",
                self.agent_name,
                exc,
            )

    def get_agent_id(self) -> str:
        """Generate a local agent ID for the ChatAgent wrapper.

        The new AzureAIClient identifies agents by name (not ID) on the server side.
        This ID is only used locally for the ChatAgent wrapper instance.
        """
        id = generate_assistant_id()
        self.logger.debug(
            "Generated local wrapper ID: %s (not a new Azure Foundry agent)", id
        )
        return id

    async def _prepare_mcp_tool(self) -> None:
        """Translate MCPConfig to a HostedMCPTool (agent_framework construct)."""
        if not self.mcp_cfg:
            return
        try:
            http_client = None
            if self.user_access_token:
                import httpx

                http_client = httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self.user_access_token}"}
                )
                self.logger.info(
                    "Forwarding user OBO token to MCP server via Authorization header"
                )
                # Register http_client in the stack so it is closed when the agent closes
                if self._stack:
                    await self._stack.enter_async_context(http_client)

            mcp_tool = MCPStreamableHTTPTool(
                name=self.mcp_cfg.name,
                description=self.mcp_cfg.description,
                url=self.mcp_cfg.url,
                http_client=http_client,
            )
            if self._stack:
                await self._stack.enter_async_context(mcp_tool)
            self.mcp_tool = mcp_tool  # Store for later use
        except Exception:
            self.mcp_tool = None


class AzureAgentBase(MCPEnabledBase):
    """
    Extends MCPEnabledBase with Azure credential + AzureAIClient contexts.
    Subclasses:
      - create or attach an Azure AI Agent definition
      - instantiate an AzureAIClient and assign to self._agent
      - optionally register themselves via agent_registry
    """

    def __init__(
        self,
        mcp: MCPConfig | None = None,
        model_deployment_name: str | None = None,
        project_endpoint: str | None = None,
        team_service: TeamService | None = None,
        team_config: TeamConfiguration | None = None,
        memory_store: DatabaseBase | None = None,
        agent_name: str | None = None,
        agent_description: str | None = None,
        agent_instructions: str | None = None,
        project_client=None,
        user_access_token: str | None = None,
    ) -> None:
        super().__init__(
            mcp=mcp,
            team_service=team_service,
            team_config=team_config,
            project_endpoint=project_endpoint,
            memory_store=memory_store,
            agent_name=agent_name,
            agent_description=agent_description,
            agent_instructions=agent_instructions,
            model_deployment_name=model_deployment_name,
            project_client=project_client,
            user_access_token=user_access_token,
        )

        self._created_ephemeral: bool = (
            False  # reserved if you add ephemeral agent cleanup
        )

    # async def open(self) -> "AzureAgentBase":
    #     if self._stack is not None:
    #         return self
    #     self._stack = AsyncExitStack()

    #     # Acquire credential
    #     self.creds = DefaultAzureCredential()
    #     if self._stack:
    #         await self._stack.enter_async_context(self.creds)
    #     # Create AgentsClient
    #     self.client = AgentsClient(
    #         endpoint=self.project_endpoint,
    #         credential=self.creds,
    #     )
    #     if self._stack:
    #         await self._stack.enter_async_context(self.client)
    #     # Prepare MCP
    #     await self._prepare_mcp_tool()

    #     # Let subclass build agent client
    #     await self._after_open()

    #     # Register agent (best effort)
    #     try:
    #         agent_registry.register_agent(self)
    #     except Exception:
    #         pass

    #     return self

    async def close(self) -> None:
        """
        Close agent client and Azure resources.
        If you implement ephemeral agent creation in subclasses, you can
        optionally delete the agent definition here.
        """
        try:
            # Close underlying client via base close
            _agent_close = getattr(self._agent, "close", None) if self._agent else None
            if callable(_agent_close):
                try:
                    result = _agent_close()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    logging.warning(
                        "Failed to close underlying agent %r: %s",
                        self._agent,
                        exc,
                        exc_info=True,
                    )

            # Unregister from registry
            try:
                agent_registry.unregister_agent(self)
            except Exception as exc:
                logging.warning(
                    "Failed to unregister agent %r from registry: %s",
                    self,
                    exc,
                    exc_info=True,
                )

            # Close credential and project client
            if self.client:
                try:
                    await self.client.close()
                except Exception as exc:
                    logging.warning(
                        "Failed to close Azure AgentsClient %r: %s",
                        self.client,
                        exc,
                        exc_info=True,
                    )
            _creds_close = getattr(self.creds, "close", None) if self.creds else None
            if callable(_creds_close):
                try:
                    result = _creds_close()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    logging.warning(
                        "Failed to close credentials %r: %s",
                        self.creds,
                        exc,
                        exc_info=True,
                    )

        finally:
            await super().close()
            self.client = None
            self.creds = None
            self.project_endpoint = None
