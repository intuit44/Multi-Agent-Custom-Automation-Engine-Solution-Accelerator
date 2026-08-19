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
from azure.ai.projects.models import CodeInterpreterTool, PromptAgentDefinition, Tool

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
        # True only when self.creds is a per-user (OBO/passthrough) credential this
        # agent created and must close. The process-scoped Managed Identity
        # credential is borrowed (owned by config) and must NOT be closed here.
        self._owns_creds = False
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

        # Use the end-user credential when a token is available so Foundry's ARA
        # can perform its OBO exchange to user-delegated tool connections
        # (e.g. agent365/WorkIQ). With ENABLE_OBO this is a real
        # OnBehalfOfCredential; otherwise it forwards the EasyAuth token verbatim.
        # Falls back to Managed Identity in local/dev (no user token present).
        if self.user_access_token and config.APP_ENV != "dev":
            # Per-user credential: this agent owns it and closes it on close().
            new_creds = config.build_user_credential(self.user_access_token)
            self.creds = new_creds
            self._owns_creds = True
            if self._stack:
                await self._stack.enter_async_context(new_creds)
        else:
            # In local dev the device-code/EasyAuth user token is NOT audienced for
            # the Foundry data plane (https://ai.azure.com), so forwarding it verbatim
            # is rejected ("audience is incorrect"). Use the shared az/Managed Identity
            # credential for the data plane — it mints a correctly-audienced token. The
            # raw user token is still forwarded to MCP in _prepare_mcp_tool (unaffected).
            # Borrow the process-scoped Managed Identity credential. It is NOT
            # entered into this agent's stack and is NOT closed by close(), so
            # closing/rebuilding this agent can never tear down the transport that
            # other in-flight tasks (e.g. a background orchestration run) share.
            new_creds = config.get_shared_async_credential()
            self.creds = new_creds
            self._owns_creds = False
        # Create AgentsClient with a custom aiohttp connector to avoid
        # 'SSL shutdown timed out' errors on long-running Foundry calls.
        # The default aiohttp ssl_shutdown_timeout is 5s which is too short
        # when the server closes the connection after a long response.
        import aiohttp

        _connector = aiohttp.TCPConnector(
            ssl_shutdown_timeout=30.0,
            enable_cleanup_closed=True,
        )
        self.client = AgentsClient(
            endpoint=self.project_endpoint or "",
            credential=new_creds,
            connection=_connector,
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
        if self._stack is None and self.mcp_tool is None:
            return
        try:
            # 1. Close the underlying AzureAIClient / ResponsesClient
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

            # 2. Close AgentsClient
            if self.client and hasattr(self.client, "close"):
                try:
                    await self.client.close()
                except Exception as exc:
                    self.logger.debug(
                        "AgentsClient close error (non-critical): %s", exc
                    )

            # 3. Close MCPStreamableHTTPTool directly (NOT via stack).
            # anyio cancel scopes must be exited from the same task that entered
            # them. We swallow RuntimeError from cross-task teardown — the HTTP
            # DELETE /mcp is still sent by the SDK before the error fires, so the
            # server-side session is cleaned up regardless.
            if self.mcp_tool is not None:
                try:
                    await self.mcp_tool.__aexit__(None, None, None)
                except RuntimeError as exc:
                    if (
                        "cancel scope" in str(exc).lower()
                        or "different task" in str(exc).lower()
                    ):
                        self.logger.debug(
                            "MCP tool cross-task teardown (expected on force-rebuild): %s",
                            exc,
                        )
                    else:
                        self.logger.warning("MCP tool close error: %s", exc)
                except Exception as exc:
                    self.logger.debug("MCP tool close error (non-critical): %s", exc)

            # 4. Release the AsyncExitStack (http_client and other non-MCP contexts).
            if self._stack:
                try:
                    await self._stack.aclose()
                except Exception as exc:
                    self.logger.debug(
                        "Stack release notice for agent '%s': %s",
                        self.agent_name,
                        exc,
                    )

            # 5. Close credential — ONLY if this agent owns it.
            if self._owns_creds and self.creds and hasattr(self.creds, "close"):
                try:
                    await self.creds.close()
                except Exception as exc:
                    self.logger.debug("Credential close error (non-critical): %s", exc)

        finally:
            try:
                agent_registry.unregister_agent(self)
            except Exception as exc:
                self.logger.debug(
                    "Agent unregister error (non-critical) for '%s': %s",
                    self.agent_name,
                    exc,
                )
            self._stack = None
            self.mcp_tool = None
            self._agent = None
            self.client = None
            self.creds = None
            self._owns_creds = False

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

    async def _register_in_foundry(
        self,
        *,
        with_code_interpreter: bool = False,
    ) -> None:
        """Persist agent definition in Azure AI Foundry via create_version.

        This ensures the agent is visible in the Foundry portal and VS Code
        extension, regardless of whether execution uses AzureOpenAIResponsesClient
        (runtime tools) or AzureAIClient (published/server-side tools).
        Called from subclasses that need to publish/refresh an agent definition.
        ``with_code_interpreter=True`` declares CodeInterpreterTool in the
        published definition so AzureAIClient(use_latest_version=True) finds
        the tool server-side. Agents composed dynamically by the Model Router
        (coding_tools=True) are never pre-configured in the Foundry portal, so
        the tool must be declared at first publish time.

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
                "✅ Agent '%s' already registered in Foundry (cached for this process). NO creating new version.",
                self.agent_name,
            )
            return

        try:
            existing_agent = None
            if not force_republish:
                self.logger.info(
                    "🔍 Checking if agent '%s' already exists in Foundry to avoid creating new version...",
                    self.agent_name,
                )
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
                    "✅ Agent '%s' ALREADY EXISTS in Foundry (id=%s, latest_version=%s, version_id=%s). "
                    "REUSING existing agent - NO NEW VERSION CREATED. "
                    "Set MACAE_FORCE_AGENT_PUBLISH=1 only if you need to update instructions.",
                    self.agent_name,
                    getattr(existing_agent, "id", "unknown"),
                    _version_str,
                    _version_id,
                )
                return

            # Only reaches here if agent does NOT exist in Foundry or MACAE_FORCE_AGENT_PUBLISH=1
            tools_to_publish: list[Tool] | None = (
                [CodeInterpreterTool()] if with_code_interpreter else None
            )
            self.logger.warning(
                "⚠️ Agent '%s' does NOT exist in Foundry OR force_republish=True. "
                "Creating NEW version (tools=%s)...",
                self.agent_name,
                "CodeInterpreter" if with_code_interpreter else "none",
            )
            agent_def = await self.project_client.agents.create_version(
                agent_name=self.agent_name,
                description=self.agent_description or "",
                definition=PromptAgentDefinition(
                    model=self.model_deployment_name or "",
                    instructions=self.agent_instructions or "",
                    tools=tools_to_publish,
                ),
            )
            self.logger.info(
                "🆕 CREATED NEW agent version '%s' in Foundry (id=%s, version=%s, tools=%s)",
                self.agent_name,
                agent_def.id,
                agent_def.version,
                "CodeInterpreter" if with_code_interpreter else "none",
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
        """Translate MCPConfig to a MCPStreamableHTTPTool and connect it.

        The tool is intentionally NOT entered into the AsyncExitStack.
        anyio cancel scopes (used internally by streamable_http_client) must be
        exited from the SAME task that entered them. Since close() can be called
        from a different task (e.g. force-rebuild in a background task), putting
        the MCP tool in the stack causes:
            RuntimeError: Attempted to exit cancel scope in a different task
        Instead we open/close the tool directly and swallow cross-task teardown
        errors — the HTTP DELETE /mcp is still sent (confirmed in logs), so the
        server-side session is cleaned up regardless.
        """
        if not self.mcp_cfg:
            return
        try:
            http_client = None
            if self.user_access_token:
                import httpx

                # Match the MCP SDK's own client timeouts (create_mcp_http_client):
                # 30s for regular ops, 300s read for the long-lived streamable-http
                # GET stream. A bare httpx.AsyncClient() defaults to read=5s, which
                # kills that GET stream on any idle >5s (approval pause / gaps
                # between agent turns) → "MCP connection closed unexpectedly.
                # Reconnecting" churn → cross-task cancel-scope teardown → the run
                # truncates. Passing our own client made us responsible for this.
                http_client = httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self.user_access_token}"},
                    timeout=httpx.Timeout(30.0, read=300.0),
                )
                self.logger.info(
                    "Forwarding user OBO token to MCP server via Authorization header"
                )
                # http_client IS entered into the stack — it has no anyio cancel
                # scope and closes cleanly from any task.
                if self._stack:
                    await self._stack.enter_async_context(http_client)

            mcp_tool = MCPStreamableHTTPTool(
                name=self.mcp_cfg.name,
                description=self.mcp_cfg.description,
                url=self.mcp_cfg.url,
                http_client=http_client,
            )
            # Open the tool directly (not via stack) to avoid cross-task
            # cancel-scope violations on close.
            await mcp_tool.__aenter__()
            self.mcp_tool = mcp_tool
        except Exception as exc:
            self.logger.warning("MCP tool setup failed: %s", exc)
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

            # Close the per-agent AgentsClient only. The credential is NOT
            # closed here: without a user token self.creds is the process-shared
            # Managed Identity credential (borrowed from config), and closing it
            # kills token minting for the whole process — blob store, Foundry
            # file uploads and download-file all fail with "HTTP transport has
            # already been closed" once their cached tokens expire. The guarded
            # close in super().close() handles the per-user (owned) credential.
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

        finally:
            await super().close()
            self.client = None
            self.project_endpoint = None
