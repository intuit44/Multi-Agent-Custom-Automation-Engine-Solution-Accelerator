"""Orchestration manager (agent_framework version) handling multi-agent Magentic workflow creation and execution."""

import asyncio
import inspect
import logging
import re
import time as _time
import uuid
from typing import List, Optional

from agent_framework import (
    Agent,
    AgentResponseUpdate,
    InMemoryCheckpointStorage,
    Message,
)

# agent_framework imports
from agent_framework_azure_ai import AzureAIClient, AzureAIProjectAgentOptions
from agent_framework_orchestrations import MagenticBuilder
from agent_framework_orchestrations._base_group_chat_orchestrator import (
    GroupChatRequestSentEvent,
    GroupChatResponseReceivedEvent,
)
from agent_framework_orchestrations._magentic import (
    MagenticProgressLedger,
)
from requests import session

from common.config.app_config import config
from common.database.database_base import DatabaseBase
from common.models.messages_af import TeamConfiguration
from v4.callbacks.response_handlers import (
    streaming_agent_response_callback,
)
from v4.common.services.team_service import TeamService
from v4.config.settings import connection_config, orchestration_config
from v4.magentic_agents.magentic_agent_factory import MagenticAgentFactory
from v4.models.messages import WebsocketMessageType
from v4.orchestration.human_approval_manager import HumanApprovalMagenticManager


class OrchestrationManager:
    """Manager for handling orchestration logic using agent_framework Magentic workflow."""

    logger = logging.getLogger(f"{__name__}.OrchestrationManager")

    # Per-user-id lock to prevent concurrent orchestration initialization
    # Ensures idempotency: only one initialization runs per user at a time
    _initialization_locks: dict[str, asyncio.Lock] = {}

    def __init__(self):
        self.user_id: Optional[str] = None
        self.logger = self.__class__.logger

    @classmethod
    def _get_user_lock(cls, user_id: str) -> asyncio.Lock:
        """Get or create an asyncio.Lock for the given user_id."""
        if user_id not in cls._initialization_locks:
            cls._initialization_locks[user_id] = asyncio.Lock()
        return cls._initialization_locks[user_id]

    def _extract_response_text(self, data) -> str:
        """
        Extract text content from various agent_framework response types.

        Handles:
        - Message: Extract .text
        - AgentResponse: Extract .text
        - AgentExecutorResponse: Extract from agent_response.text or full_conversation[-1].text
        - List of any of the above
        """
        if data is None:
            return ""

        # Direct Message
        if isinstance(data, Message):
            return data.text or ""

        # Has .text attribute directly (AgentResponse, etc.)
        if hasattr(data, "text") and data.text:
            return data.text

        # AgentExecutorResponse - has agent_response and full_conversation
        if hasattr(data, "agent_response"):
            # Try to get text from agent_response first
            agent_resp = data.agent_response
            if agent_resp and hasattr(agent_resp, "text") and agent_resp.text:
                return agent_resp.text
            # Fallback to last message in full_conversation
            if hasattr(data, "full_conversation") and data.full_conversation:
                last_msg = data.full_conversation[-1]
                if isinstance(last_msg, Message) and last_msg.text:
                    return last_msg.text

        # List of items - could be AgentExecutorResponse, ChatMessage, etc.
        if isinstance(data, list) and len(data) > 0:
            texts = []
            for item in data:
                # Recursively extract from each item
                item_text = self._extract_response_text(item)
                if item_text:
                    texts.append(item_text)
            if texts:
                # Return the last non-empty response (most recent)
                return texts[-1]

        return ""

    # ---------------------------
    # Orchestration construction
    # ---------------------------
    @classmethod
    async def init_orchestration(
        cls,
        agents: List,
        team_config: TeamConfiguration,
        memory_store: DatabaseBase,
        user_id: str | None = None,
    ):
        """
        Initialize a Magentic workflow with:
          - Provided agents (participants)
          - HumanApprovalMagenticManager as orchestrator manager
          - AzureAIClient as the underlying chat client
          - Event-based callbacks for streaming and final responses
        - Uses same deployment, endpoint, and credentials
        - Applies same execution settings (temperature, max_tokens)
        - Maintains same human approval workflow
        """
        if not user_id:
            raise ValueError("user_id is required to initialize orchestration")

        # Get credential from config (same as old version)
        credential = config.get_azure_credential(client_id=config.AZURE_CLIENT_ID)

        raw_name = team_config.name if team_config.name else "OrchestratorAgent"
        # Replace spaces and invalid chars with hyphens, strip leading/trailing hyphens
        sanitized_name = re.sub(r"[^a-zA-Z0-9-]", "-", raw_name)
        sanitized_name = re.sub(r"-+", "-", sanitized_name)  # Collapse multiple hyphens
        sanitized_name = sanitized_name.strip("-")[:63]  # Trim and limit length
        agent_name = sanitized_name if sanitized_name else "OrchestratorAgent"

        try:
            # Create the chat client (AzureAIClient)
            chat_client = AzureAIClient(
                project_endpoint=config.AZURE_AI_PROJECT_ENDPOINT,
                model_deployment_name=team_config.deployment_name,
                agent_name=agent_name,
                credential=credential,
            )

            # New API: Create an Agent to wrap the chat client for the manager
            manager_agent = Agent(
                client=chat_client,
                name="MagenticManager",
                default_options=AzureAIProjectAgentOptions(
                    store=True
                ),  # Foundry persists conversation so the published agent keeps context across rounds
            )

            cls.logger.info(
                "Created AzureAIClient and manager Agent for orchestration with model '%s' at endpoint '%s'",
                team_config.deployment_name,
                config.AZURE_AI_PROJECT_ENDPOINT,
            )
        except Exception as e:
            cls.logger.error("Failed to create AzureAIClient: %s", e)
            raise

        # Create HumanApprovalMagenticManager with the manager agent
        # New API: StandardMagenticManager takes agent as first positional argument
        try:
            manager = HumanApprovalMagenticManager(
                user_id=user_id,
                agent=manager_agent,  # New API: pass agent instead of chat_client
                max_round_count=orchestration_config.max_rounds,
                max_stall_count=3,
                max_reset_count=2,
            )
            cls.logger.info(
                "Created HumanApprovalMagenticManager for user '%s' with max_rounds=%d",
                user_id,
                orchestration_config.max_rounds,
            )
        except Exception as e:
            cls.logger.error("Failed to create manager: %s", e)
            raise

        # Build participant map: use each agent's name as key
        participants = {}
        for ag in agents:
            name = getattr(ag, "agent_name", None) or getattr(ag, "name", None)
            if not name:
                name = f"agent_{len(participants) + 1}"

            # Extract the inner Agent for wrapper templates
            # FoundryAgentTemplate wrap an Agent in self._agent
            # ProxyAgent directly extends BaseAgent and can be used as-is
            if hasattr(ag, "_agent") and ag._agent is not None:
                # This is a wrapper (FoundryAgentTemplate)
                # Use the inner Agent which implements AgentProtocol
                participants[name] = ag._agent
                cls.logger.debug("Added participant '%s' (extracted inner agent)", name)
            else:
                # This is already an agent (like ProxyAgent extending BaseAgent)
                participants[name] = ag
                cls.logger.debug("Added participant '%s'", name)

        # Assemble workflow with callback
        storage = InMemoryCheckpointStorage()

        # New SDK: participants() accepts a Sequence (list) of agents
        # The orchestrator uses agent.name to identify them
        participant_list = list(participants.values())
        cls.logger.info("Participants for workflow: %s", list(participants.keys()))

        # Note: When using a custom manager, the framework ignores max_round_count,
        # max_stall_count, and intermediate_outputs parameters.
        # These are already configured in the HumanApprovalMagenticManager.
        builder = MagenticBuilder(
            participants=participant_list,
            manager=manager,
            checkpoint_storage=storage,
            intermediate_outputs=True,  # Required: yield agent streaming output events, not just orchestrator output
        )

        # Build workflow
        workflow = builder.build()
        cls.logger.info(
            "Built Magentic workflow with %d participants and event callbacks",
            len(participants),
        )

        return workflow

    # ---------------------------
    # Orchestration retrieval
    # ---------------------------
    @classmethod
    async def get_current_or_new_orchestration(
        cls,
        user_id: str,
        team_config: TeamConfiguration,
        team_switched: bool,
        team_service: Optional[TeamService] = None,
        force_rebuild: bool = False,
        user_access_token: Optional[str] = None,
    ):
        """
        Return an existing workflow for the user or create a new one if:
          - None exists
          - Team switched flag is True
          - force_rebuild is True (for new tasks after workflow completion)

        Thread-safe: uses per-user-id lock to prevent concurrent initialization.
        If multiple requests arrive simultaneously, only the first one initializes;
        the others wait and return the same orchestration instance.
        """
        # Acquire per-user lock to ensure only one initialization runs at a time
        user_lock = cls._get_user_lock(user_id)
        async with user_lock:
            # Double-check pattern: re-check after acquiring lock
            current = orchestration_config.get_current_orchestration(user_id)
            needs_rebuild = current is None or team_switched or force_rebuild

            if needs_rebuild:
                if team_service is None or team_service.memory_context is None:
                    raise ValueError(
                        "team_service with initialized memory_context is required"
                    )

                if current is not None and (team_switched or force_rebuild):
                    reason = (
                        "team switched"
                        if team_switched
                        else "force rebuild for new task"
                    )
                    cls.logger.info(
                        "Rebuilding orchestration for user '%s' (reason: %s)",
                        user_id,
                        reason,
                    )

                    prior_wrappers = orchestration_config.agent_wrappers.pop(
                        user_id, []
                    )

                    async def _close_wrapper(agent) -> None:
                        agent_name = getattr(
                            agent, "agent_name", getattr(agent, "name", "")
                        )
                        close_method = getattr(agent, "close", None)
                        if callable(close_method) and inspect.iscoroutinefunction(
                            close_method
                        ):
                            try:
                                await close_method()
                                cls.logger.debug(
                                    "Closed agent wrapper '%s'", agent_name
                                )
                            except Exception as e:
                                cls.logger.warning(
                                    "Non-fatal error closing agent wrapper '%s': %s",
                                    agent_name,
                                    e,
                                )

                    if prior_wrappers:
                        close_tasks = [
                            asyncio.ensure_future(_close_wrapper(a))
                            for a in prior_wrappers
                        ]
                        await asyncio.gather(*close_tasks, return_exceptions=True)

                factory = MagenticAgentFactory(team_service=team_service)
                try:
                    agents = await factory.get_agents(
                        user_id=user_id,
                        team_config_input=team_config,
                        memory_store=team_service.memory_context,
                        # OBO: agents authenticate as the user (build_user_credential
                        # → OnBehalfOfCredential). With offline_access the credential
                        # self-renews downstream, so it survives long/approval-gated
                        # runs without recreating agents. Falls back to app MI if None.
                        user_access_token=user_access_token,
                    )
                    cls.logger.info(
                        "Created %d agents for user '%s'", len(agents), user_id
                    )
                except Exception as e:
                    cls.logger.error(
                        "Failed to create agents for user '%s': %s", user_id, e
                    )
                    raise
                try:
                    cls.logger.info(
                        "Initializing new orchestration for user '%s'", user_id
                    )
                    workflow = await cls.init_orchestration(
                        agents, team_config, team_service.memory_context, user_id
                    )
                    orchestration_config.orchestrations[user_id] = workflow
                    # Store wrappers for proper cleanup on next rebuild
                    orchestration_config.agent_wrappers[user_id] = agents
                except Exception as e:
                    cls.logger.error(
                        "Failed to initialize orchestration for user '%s': %s",
                        user_id,
                        e,
                    )
                    raise
            return orchestration_config.get_current_orchestration(user_id)

    # ---------------------------
    # Execution
    # ---------------------------
    async def _persist_agent_message(
        self,
        *,
        plan_id: Optional[str],
        user_id: str,
        agent_name: str,
        content: str,
        is_final: bool,
    ) -> None:
        """Persist an agent message to the plan store from the backend.

        Reuses the SAME proven path the ``/agent_message`` endpoint used
        (``PlanService.handle_agent_messages``) — just triggered in-process
        instead of by the frontend echo. On ``is_final`` it also flips the plan
        to completed. Never raises: persistence must not abort the run.
        """
        if not plan_id:
            return
        try:
            from v4.common.services.plan_service import PlanService
            from v4.models.messages import AgentMessageResponse, AgentMessageType

            resp = AgentMessageResponse(
                plan_id=plan_id,
                agent=agent_name,
                content=content,
                agent_type=AgentMessageType.AI_AGENT,
                is_final=is_final,
                streaming_message=content if is_final else None,
            )
            await PlanService.handle_agent_messages(resp, user_id)
        except Exception as _pe:
            self.logger.warning(
                "Backend persist of agent message (plan=%s, final=%s) failed: %s",
                plan_id,
                is_final,
                _pe,
            )

    async def run_orchestration(
        self, user_id, session_id: str, input_task, plan_id: Optional[str] = None
    ) -> None:
        """
        Execute the Magentic workflow for the provided user and task description.

        ``plan_id`` makes the BACKEND the single source of truth for plan state:
        agent messages are persisted to the plan store and the plan is marked
        completed here, in-process — no longer dependent on the frontend echoing
        to ``/agent_message`` (which was fragile and duplicated the final).
        """
        job_id = str(uuid.uuid4())
        orchestration_config.set_approval_pending(job_id)
        self.logger.info(
            "Starting orchestration job '%s' for user '%s'", job_id, user_id
        )

        workflow = orchestration_config.get_current_orchestration(user_id)
        if workflow is None:
            raise ValueError("Orchestration not initialized for user.")
        # Fresh thread per participant to avoid cross-run state bleed
        executors = getattr(workflow, "executors", {})
        self.logger.debug("Executor keys at run start: %s", list(executors.keys()))

        for exec_key, executor in executors.items():
            try:
                if exec_key == "magentic_orchestrator":
                    # Orchestrator path
                    if hasattr(executor, "_conversation"):
                        conv = getattr(executor, "_conversation")
                        # Support list-like or custom container with clear()
                        if hasattr(conv, "clear") and callable(conv.clear):
                            conv.clear()
                            self.logger.debug(
                                "Cleared orchestrator conversation (%s)", exec_key
                            )
                        elif isinstance(conv, list):
                            conv[:] = []
                            self.logger.debug(
                                "Emptied orchestrator conversation list (%s)", exec_key
                            )
                        else:
                            self.logger.debug(
                                "Orchestrator conversation not clearable type (%s): %s",
                                exec_key,
                                type(conv),
                            )
                    else:
                        self.logger.debug(
                            "Orchestrator has no _conversation attribute (%s)", exec_key
                        )
                else:
                    # Agent path
                    if hasattr(executor, "_chat_history"):
                        hist = getattr(executor, "_chat_history")
                        if hasattr(hist, "clear") and callable(hist.clear):
                            hist.clear()
                            self.logger.debug(
                                "Cleared agent chat history (%s)", exec_key
                            )
                        elif isinstance(hist, list):
                            hist[:] = []
                            self.logger.debug(
                                "Emptied agent chat history list (%s)", exec_key
                            )
                        else:
                            self.logger.debug(
                                "Agent chat history not clearable type (%s): %s",
                                exec_key,
                                type(hist),
                            )
                    else:
                        self.logger.debug(
                            "Agent executor has no _chat_history attribute (%s)",
                            exec_key,
                        )
            except Exception as e:
                self.logger.warning(
                    "Failed clearing state for executor %s: %s", exec_key, e
                )
        # --- END NEW BLOCK ---

        # Build task from input. Prior conversation context (recovered from the
        # single source of truth at the Plan boundary) rides inside the task string
        # so the Magentic manager plans WITH context instead of from a bare goal.
        # Cross-run bleed was already cleared above; this is the CURRENT session's
        # grounding, seeded deliberately.
        task_text = getattr(input_task, "description", str(input_task))
        _context = getattr(input_task, "context", "") or ""
        if _context:
            task_text = f"{_context}\n\n---\n\nCurrent objective:\n{task_text}"
            self.logger.info(
                "Seeded orchestration task with %d chars of session context",
                len(_context),
            )
        self.logger.debug("Task: %s", task_text)

        # ── Stamp session_id on ProxyAgent instances so they can write to chat_cosmos ──
        from v4.magentic_agents.proxy_agent import ProxyAgent as _ProxyAgent

        for _agent in orchestration_config.agent_wrappers.get(user_id, []):
            if isinstance(_agent, _ProxyAgent):
                _agent.session_id = session_id

        # Track how many times each agent is called (for debugging duplicate calls)
        agent_call_counts: dict = {}
        # Buffer streamed text per-agent so we can emit a complete AGENT_MESSAGE
        agent_stream_buffers: dict[str, str] = {}

        agents_actively_responding: set[str] = set()

        try:
            # Execute workflow using run() with stream=True
            # The execution settings are configured in the manager/client
            final_output: str | None = None

            self.logger.info("Starting workflow execution...")

            async for event in workflow.run(task_text, stream=True):
                try:
                    # WorkflowEvent has a .type field (string) instead of specific event classes
                    event_type = (
                        event.type if hasattr(event, "type") else type(event).__name__
                    )
                    if event_type not in ("status", "output"):
                        self.logger.info("[EVENT] type=%s", event_type)

                    # Handle orchestrator events (plan, progress ledger)
                    if event_type == "magentic_orchestrator":
                        self.logger.info("[Magentic Orchestrator Event]")
                        if isinstance(event.data, Message):
                            self.logger.info(
                                "Plan message: %s",
                                event.data.text[:200] if event.data.text else "",
                            )
                        elif isinstance(event.data, MagenticProgressLedger):
                            self.logger.info("Progress ledger received")

                    # Handle group chat request sent
                    elif event_type == "group_chat":
                        # Check if this is a request or response via the data type
                        if isinstance(event.data, GroupChatRequestSentEvent):
                            agent_name = event.data.participant_name
                            agent_call_counts[agent_name] = (
                                agent_call_counts.get(agent_name, 0) + 1
                            )
                            call_num = agent_call_counts[agent_name]

                            self.logger.info(
                                "[REQUEST SENT (round %d)] to agent: %s (call #%d)",
                                event.data.round_index,
                                agent_name,
                                call_num,
                            )

                            if call_num > 1:
                                self.logger.warning(
                                    "Agent '%s' called %d times", agent_name, call_num
                                )

                            # Open the response window: clear any residual buffer from a
                            # previous round and mark this agent as the active speaker.
                            # Only chunks received while an agent is in this set will be
                            # forwarded to the UI; broadcast-sync chunks (should_respond=False)
                            # arrive outside this window and are silently discarded.
                            agent_stream_buffers.pop(agent_name, None)
                            agents_actively_responding.add(agent_name)

                        elif isinstance(event.data, GroupChatResponseReceivedEvent):
                            agent_name = event.data.participant_name
                            self.logger.info(
                                "[RESPONSE RECEIVED (round %d)] from agent: %s",
                                event.data.round_index,
                                agent_name,
                            )
                            # Close the response window: no more chunks for this agent
                            # should reach the UI until the next RequestSentEvent.
                            agents_actively_responding.discard(agent_name)
                            # Flush accumulated streaming content as a complete AGENT_MESSAGE
                            buffered = agent_stream_buffers.pop(agent_name, "")
                            if buffered:
                                from v4.callbacks.response_handlers import (
                                    clean_citations,
                                )
                                from v4.models.messages import AgentMessage

                                cleaned = clean_citations(buffered)
                                if cleaned.strip():
                                    agent_msg = AgentMessage(
                                        agent_name=agent_name,
                                        timestamp=str(_time.time()),
                                        content=cleaned,
                                    )
                                    await connection_config.send_status_update_async(
                                        agent_msg,
                                        user_id,
                                        message_type=WebsocketMessageType.AGENT_MESSAGE,
                                    )
                                    self.logger.info(
                                        "Sent AGENT_MESSAGE for '%s' (%d chars)",
                                        agent_name,
                                        len(cleaned),
                                    )
                                    # Persist this intermediate agent message to the
                                    # plan store from the BACKEND (single source of
                                    # truth) instead of relying on the frontend echo.
                                    # is_final=False → background detail; the chat
                                    # thread only ever gets the ONE final (writeback).
                                    await self._persist_agent_message(
                                        plan_id=plan_id,
                                        user_id=user_id,
                                        agent_name=agent_name,
                                        content=cleaned,
                                        is_final=False,
                                    )

                    # Handle executor completed - just log, don't send to UI
                    elif event_type == "executor_completed":
                        self.logger.debug(
                            "[EXECUTOR COMPLETED] agent: %s",
                            getattr(event, "executor_id", "unknown"),
                        )
                        # Don't send to UI here - group_chat events already handle agent messages

                    # Handle workflow output event (streaming chunks AND final result)
                    elif event_type == "output":
                        executor_id = getattr(event, "executor_id", None)
                        output_data = event.data
                        # Streaming AgentResponseUpdate chunks emit per-token —
                        # log at DEBUG to avoid flooding INFO logs (hundreds per response).
                        # Other output types (final results, etc.) keep INFO.
                        _data_type_name = type(output_data).__name__
                        if _data_type_name == "AgentResponseUpdate":
                            self.logger.debug(
                                "[OUTPUT] executor=%s data_type=%s",
                                executor_id,
                                _data_type_name,
                            )
                        else:
                            self.logger.info(
                                "[OUTPUT] executor=%s data_type=%s",
                                executor_id,
                                _data_type_name,
                            )

                        # Streaming chunk from an agent executor.
                        # Only process chunks for agents that are within a formal
                        # RequestSent → ResponseReceived window.  Chunks emitted by
                        # the SDK's internal broadcast (should_respond=False) arrive
                        # outside that window and are discarded here, preventing the
                        # duplicate-message problem without altering SDK behaviour.
                        if isinstance(output_data, AgentResponseUpdate) and executor_id:
                            self.logger.debug(
                                "[OUTPUT] executor_id='%s' actively_responding=%s",
                                executor_id,
                                agents_actively_responding,
                            )
                            if executor_id not in agents_actively_responding:
                                self.logger.debug(
                                    "[OUTPUT] Discarding broadcast chunk from '%s' "
                                    "(outside active-response window)",
                                    executor_id,
                                )
                            else:
                                chunk_text = output_data.text or ""
                                if chunk_text:
                                    agent_stream_buffers[executor_id] = (
                                        agent_stream_buffers.get(executor_id, "")
                                        + chunk_text
                                    )
                                try:
                                    await streaming_agent_response_callback(
                                        executor_id,
                                        output_data,
                                        False,
                                        user_id,
                                    )
                                except Exception as e:
                                    self.logger.error(
                                        "Error in streaming callback for agent %s: %s",
                                        executor_id,
                                        e,
                                    )
                        # Final workflow output (list[Message] or Message)
                        elif isinstance(output_data, Message):
                            final_output = output_data.text or ""
                        elif isinstance(output_data, list):
                            # Handle list of Message objects
                            texts = []
                            for item in output_data:
                                if isinstance(item, Message):
                                    if item.text:
                                        texts.append(item.text)
                                else:
                                    texts.append(str(item))
                            final_output = "\n".join(texts)
                        elif hasattr(output_data, "text"):
                            final_output = output_data.text or ""
                        else:
                            final_output = str(output_data) if output_data else ""
                        self.logger.debug("Received workflow output event")

                except Exception as e:
                    self.logger.error(
                        f"Error processing event {type(event).__name__}: {e}",
                        exc_info=True,
                    )

            # Extract final result
            final_text = final_output if final_output else ""

            # Log agent call summary
            self.logger.info("Agent call counts: %s", agent_call_counts)

            # Log results
            self.logger.info("\nAgent responses:")
            self.logger.info(
                "Orchestration completed. Final result length: %d chars",
                len(final_text),
            )
            self.logger.info("\nFinal result:\n%s", final_text)
            self.logger.info("=" * 50)

            # Send final result via WebSocket
            await connection_config.send_status_update_async(
                {
                    "type": WebsocketMessageType.FINAL_RESULT_MESSAGE,
                    "data": {
                        "content": final_text,
                        "status": "completed",
                        "timestamp": asyncio.get_event_loop().time(),
                    },
                },
                user_id,
                message_type=WebsocketMessageType.FINAL_RESULT_MESSAGE,
            )
            self.logger.info("Final result sent via WebSocket to user '%s'", user_id)

            # ── Persist final + mark plan completed (BACKEND-owned) ───────────
            # is_final=True routes through PlanService.handle_agent_messages, which
            # persists the final agent message to the plan store AND flips the plan
            # to overall_status=completed — the state that USED to happen only when
            # the frontend echoed the final. Now it is guaranteed, headless.
            if final_text:
                await self._persist_agent_message(
                    plan_id=plan_id,
                    user_id=user_id,
                    agent_name="Group_Chat_Manager",
                    content=final_text,
                    is_final=True,
                )

            # ── Write Plan result back to chat session ────────────────────────
            # This closes the visibility gap: after Plan execution the chat
            # agent will have the Plan outcome in its Cosmos history so the
            # user can continue the conversation with full context.
            if final_text and session:
                try:
                    from common.services.chat_cosmos_service import (
                        get_chat_cosmos_service,
                    )

                    _chat_svc_wb = await get_chat_cosmos_service()
                    await _chat_svc_wb.add_message(
                        session_id=session_id,
                        user_id=user_id,
                        content=final_text,
                        role="assistant",
                        metadata={"intent": "task", "type": "plan_result"},
                    )
                    self.logger.info(
                        "Plan result written back to chat session %s (%d chars)",
                        session_id[:12],
                        len(final_text),
                    )
                except Exception as _wb_err:
                    self.logger.warning(
                        "Could not write Plan result to chat session %s: %s",
                        session_id,
                        _wb_err,
                    )

        except Exception as e:
            # Approval timeout / rejection is an expected flow in HITL.
            # Do not bubble it as an unhandled server error.
            if str(e) == "Plan execution cancelled by user":
                self.logger.warning(
                    "Orchestration cancelled for user '%s' due to missing/negative approval",
                    user_id,
                )
                try:
                    await connection_config.send_status_update_async(
                        {
                            "type": WebsocketMessageType.FINAL_RESULT_MESSAGE,
                            "data": {
                                "content": "Plan execution cancelled by user or approval timeout.",
                                "status": "cancelled",
                                "timestamp": asyncio.get_event_loop().time(),
                            },
                        },
                        user_id,
                        message_type=WebsocketMessageType.FINAL_RESULT_MESSAGE,
                    )
                except Exception as send_error:
                    self.logger.error(
                        "Failed to send cancellation status: %s", send_error
                    )
                return

            # Error handling
            self.logger.error("Unexpected orchestration error: %s", e, exc_info=True)
            self.logger.error("Error type: %s", type(e).__name__)
            if hasattr(e, "__dict__"):
                self.logger.error("Error attributes: %s", e.__dict__)
            self.logger.info("=" * 50)

            # Send error status to user
            try:
                await connection_config.send_status_update_async(
                    {
                        "type": WebsocketMessageType.FINAL_RESULT_MESSAGE,
                        "data": {
                            "content": f"Error during orchestration: {str(e)}",
                            "status": "error",
                            "timestamp": asyncio.get_event_loop().time(),
                        },
                    },
                    user_id,
                    message_type=WebsocketMessageType.FINAL_RESULT_MESSAGE,
                )
            except Exception as send_error:
                self.logger.error("Failed to send error status: %s", send_error)
            raise
