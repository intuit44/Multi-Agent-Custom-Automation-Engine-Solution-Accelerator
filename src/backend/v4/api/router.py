import asyncio
import json
import logging
import os
import uuid
from contextlib import AsyncExitStack
from typing import Annotated, Any, Optional, cast

from azure.core.exceptions import ResourceNotFoundError
from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from opentelemetry import trace
from pydantic import BaseModel

import v4.models.messages as messages
from auth.auth_utils import get_authenticated_user_details
from common.database.database_factory import DatabaseFactory
from common.models.messages_af import (
    ChatMessageRequest,
    ChatMessageResponse,
    InitTeamQuery,
    InputTask,
    Plan,
    PlanStatus,
    ResumePlanRequest,
    TeamAgent,
    TeamConfiguration,
    TeamSelectionRequest,
)
from common.services.chat_cosmos_service import get_chat_cosmos_service
from common.utils.event_utils import track_event_if_configured
from common.utils.utils_af import (
    find_first_available_team,
    rai_success,
    rai_validate_team_config,
)
from v4.common.models.mcp_connection_models import (
    McpReadResourceRequest,
    MCPServerEntry,
    MCPServerUpdateRequest,
    OAuthCallbackQuery,
)
from v4.common.services.plan_service import PlanService
from v4.common.services.team_service import TeamService
from v4.common.tool_errors import (
    ToolError,
    ToolErrorCategory,
    classify_tool_error,
    emit_tool_error,
    run_with_backoff,
    user_message_for,
)
from v4.config.settings import (
    connection_config,
    orchestration_config,
    team_config,
)
from v4.models.messages import WebsocketMessageType
from v4.orchestration.orchestration_manager import OrchestrationManager

router = APIRouter()
logger = logging.getLogger(__name__)


def _extract_auth(request: Request) -> tuple:
    """Extract (user_id, tenant_id) from request headers.

    Single point of auth extraction for all endpoints.
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    tenant_id = authenticated_user.get("tenant_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="no user found")
    return user_id, tenant_id


def _extract_auth_with_token(request: Request) -> tuple:
    """Extract (user_id, tenant_id, access_token) from request headers.

    Use this for endpoints that need the user's access token for OBO flow.
    """
    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    tenant_id = authenticated_user.get("tenant_id", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="no user found")
    return user_id, tenant_id, authenticated_user.get("access_token")


app_v4 = APIRouter(
    prefix="/api/v4",
    responses={
        # FastAPI answers 400 "There was an error parsing the body" on
        # malformed JSON for EVERY body-taking route — framework behavior,
        # so the contract must declare it (schemathesis: UndefinedStatusCode).
        400: {"description": "Malformed request body"},
        404: {"description": "Not found"},
    },
)

# Workspace endpoints: Monaco (browser) and MCP filesystem agents share one physical
# path — {MACAE_WORKSPACE_ROOT}/{user_id}/{workspace_id}/ — so there is a single
# source of truth per workspace regardless of which writer touches it.
from v4.api.workspace_router import workspace_router, workspaces_router  # noqa: E402

app_v4.include_router(workspaces_router)
app_v4.include_router(workspace_router)


@app_v4.websocket("/socket/{process_id}")
async def start_comms(
    websocket: WebSocket, process_id: str, user_id: str = Query(None)
):
    """Web-Socket endpoint for real-time process status updates."""

    # Always accept the WebSocket connection first
    await websocket.accept()

    user_id = user_id or "00000000-0000-0000-0000-000000000000"

    # Manually create a span for WebSocket since excluded_urls suppresses auto-instrumentation.
    # Without this, all track_event_if_configured calls inside WebSocket would get operation_Id = 0.
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span(
        "WebSocket_Connection",
        attributes={"process_id": process_id, "user_id": user_id},
    ) as ws_span:
        # Resolve session_id from plan for telemetry
        session_id = None
        try:
            memory_store = await DatabaseFactory.get_database(
                user_id=user_id, tenant_id=""
            )
            plan = await memory_store.get_plan_by_plan_id(plan_id=process_id)
            if plan:
                session_id = getattr(plan, "session_id", None)
                if session_id:
                    ws_span.set_attribute("session_id", session_id)
        except Exception as e:
            logging.warning(f"[websocket] Failed to resolve session_id: {e}")

        # Add to the connection manager for backend updates
        connection_config.add_connection(
            process_id=process_id, connection=websocket, user_id=user_id
        )
        ws_props = {"process_id": process_id, "user_id": user_id}
        if session_id:
            ws_props["session_id"] = session_id
        track_event_if_configured("WebSocket_Connected", ws_props)

        # Re-send any pending plan approval that was missed before WS connected
        # (fixes race condition: backend sends PLAN_APPROVAL_REQUEST before frontend connects WS)
        try:
            for m_plan_id, mplan in orchestration_config.plans.items():
                if (
                    getattr(mplan, "user_id", None) == user_id
                    and orchestration_config.approvals.get(m_plan_id) is None
                ):
                    approval_message = messages.PlanApprovalRequest(
                        plan=mplan,
                        status=messages.PlanStatus.PENDING_APPROVAL,
                        context={},
                    )
                    await connection_config.send_status_update_async(
                        message=approval_message,
                        user_id=user_id,
                        message_type=messages.WebsocketMessageType.PLAN_APPROVAL_REQUEST,
                    )
                    logging.info(
                        "Re-sent pending PLAN_APPROVAL_REQUEST for plan %s to user %s",
                        m_plan_id,
                        user_id,
                    )
                    break  # one pending plan at a time per user
        except Exception as e:
            logging.warning("Failed to re-send pending approval on WS connect: %s", e)

        # Keep the connection open - FastAPI will close the connection if this returns
        try:
            # Keep the connection open - FastAPI will close the connection if this returns
            while True:
                # no expectation that we will receive anything from the client but this keeps
                # the connection open and does not take cpu cycle
                try:
                    message = await websocket.receive_text()
                    logging.debug(
                        f"Received WebSocket message from {user_id}: {message}"
                    )
                except asyncio.TimeoutError:
                    # Ignore timeouts to keep the WebSocket connection open, but avoid a tight loop.
                    logging.debug(
                        f"WebSocket receive timeout for user {user_id}, process {process_id}"
                    )
                    await asyncio.sleep(0.1)
                except WebSocketDisconnect:
                    dc_props = {"process_id": process_id, "user_id": user_id}
                    if session_id:
                        dc_props["session_id"] = session_id
                    track_event_if_configured("WebSocket_Disconnected", dc_props)
                    logging.info(f"Client disconnected from batch {process_id}")
                    break
        except Exception as e:
            # Fixed logging syntax - removed the error= parameter
            logging.error(f"Error in WebSocket connection: {str(e)}")
        finally:
            # Always clean up the connection
            await connection_config.close_connection(process_id=process_id)


@app_v4.get("/init_team")
async def init_team(
    request: Request,
    query: Annotated[InitTeamQuery, Query()],
):
    """Initialize the user's current team of agents"""
    team_switched = query.team_switched

    # Get first available team from 4 to 1 (RFP -> Retail -> Marketing -> HR)
    # Falls back to HR if no teams are available.
    print(f"Init team called, team_switched={team_switched}")
    try:
        user_id, tenant_id, user_access_token = _extract_auth_with_token(request)

        # Initialize memory store and service
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team_service = TeamService(memory_store)

        init_team_id = await find_first_available_team(team_service, user_id)

        # Get current team if user has one
        user_current_team = await memory_store.get_current_team(user_id=user_id)

        # If no teams available and no current team, return empty state to allow custom team upload
        if not init_team_id and not user_current_team:
            print("No teams found in database. System ready for custom team upload.")
            return {
                "status": "No teams configured. Please upload a team configuration to get started.",
                "team_id": None,
                "team": None,
                "requires_team_upload": True,
            }

        # Use current team if available, otherwise use found team
        if user_current_team:
            init_team_id = user_current_team.team_id
            print(f"Using user's current team: {init_team_id}")
        elif init_team_id:
            print(f"Using first available team: {init_team_id}")
            user_current_team = await team_service.handle_team_selection(
                user_id=user_id, team_id=init_team_id
            )
            if user_current_team:
                init_team_id = user_current_team.team_id

        # Verify the team exists and user has access to it
        if not init_team_id:
            return {
                "status": "No team selected. Please select or upload a team configuration.",
                "team_id": None,
                "team": None,
                "requires_team_upload": True,
            }
        team_configuration = await team_service.get_team_configuration(
            init_team_id, user_id
        )
        if team_configuration is None:
            # If team doesn't exist, clear current team and return empty state
            await memory_store.delete_current_team(user_id)
            print(
                f"Team configuration '{init_team_id}' not found. Cleared current team."
            )
            return {
                "status": "Current team configuration not found. Please select or upload a team configuration.",
                "team_id": None,
                "team": None,
                "requires_team_upload": True,
            }

        # Set as current team in memory
        team_config.set_current_team(
            user_id=user_id, team_configuration=team_configuration
        )

        # Initialize agent team for this user session
        await OrchestrationManager.get_current_or_new_orchestration(
            user_id=user_id,
            team_config=team_configuration,
            team_switched=team_switched,
            team_service=team_service,
            user_access_token=user_access_token,  # OBO: run agents as the user
        )

        return {
            "status": "Request started successfully",
            "team_id": init_team_id,
            "team": team_configuration,
        }

    except Exception as e:
        track_event_if_configured(
            "Error_Init_Team_Failed",
            {
                "error": str(e),
            },
        )
        raise HTTPException(
            status_code=400, detail=f"Error starting request: {e}"
        ) from e


@app_v4.post("/process_request")
async def process_request(
    background_tasks: BackgroundTasks, input_task: InputTask, request: Request
):
    """
    Create a new plan without full processing.

    ---
    tags:
      - Plans
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
      - name: body
        in: body
        required: true
        schema:
          type: object
          properties:
            session_id:
              type: string
              description: Session ID for the plan
            description:
              type: string
              description: The task description to validate and create plan for
    responses:
      200:
        description: Plan created successfully
        schema:
          type: object
          properties:
            plan_id:
              type: string
              description: The ID of the newly created plan
            status:
              type: string
              description: Success message
            session_id:
              type: string
              description: Session ID associated with the plan
      400:
        description: RAI check failed or invalid input
        schema:
          type: object
          properties:
            detail:
              type: string
              description: Error message
    """
    user_id, tenant_id, user_access_token = _extract_auth_with_token(request)

    if not input_task.session_id:
        input_task.session_id = str(uuid.uuid4())

    # Attach session_id to current span for Application Insights
    span = trace.get_current_span()
    if span:
        span.set_attribute("session_id", input_task.session_id)

    plan_id = await _create_plan_and_start(
        background_tasks=background_tasks,
        user_id=user_id,
        tenant_id=tenant_id,
        user_access_token=user_access_token,
        description=input_task.description,
        session_id=input_task.session_id,
        persist_user_task=True,
        workspace_id=input_task.workspace_id,
    )
    return {
        "status": "Request started successfully",
        "session_id": input_task.session_id,
        "plan_id": plan_id,
    }


# Hard ceiling on a Router-composed roster. Magentic broadcasts every turn to
# every participant, so cost and round count grow with the roster; a task that
# genuinely needs more is a task that needs splitting.
_COMPOSED_TEAM_MAX_AGENTS = 4


async def _team_from_router_roster(
    roster: list, description: str, user_id: str, memory_store: Any
) -> TeamConfiguration:
    """Turn the Model Router's ``run_plan`` roster into a persisted team.

    Reuses the SAME validator and persistence as the upload path
    (``TeamService.validate_and_parse_team_config`` / ``save_team_configuration``)
    — the roster is just another team-config source. Persisted because
    ``plan.team_id`` is resolved later by the in-plan chat lane and
    ``resume_plan``; an unpersisted team would 404 there.

    Factory constraints are re-checked HERE, in code (the prompt orients the
    Router; it guarantees nothing): supported deployment, reasoning XOR coding
    tools, no RAG (no index to point at), reserved/duplicate names dropped,
    ProxyAgent appended (the human-in-the-loop clarification channel the
    factory special-cases by name). The team NAME is fixed: it becomes the
    Magentic manager's Foundry agent name (``sanitize(team.name)`` in
    ``init_orchestration``), and a per-task name would publish a new manager
    definition per request — version sprawl.
    """
    from common.config.app_config import config

    try:
        supported = json.loads(config.SUPPORTED_MODELS)
    except (TypeError, ValueError):
        supported = []
    deployment = config.AZURE_OPENAI_DEPLOYMENT_NAME
    if supported and deployment not in supported:
        deployment = str(supported[0])

    agents: list[dict] = []
    seen: set[str] = set()
    for raw in roster:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        # isidentifier(): no spaces/punctuation — the name becomes a Foundry
        # agent name and the Magentic participant key.
        if not name or not name.isidentifier():
            continue
        lowered = name.lower()
        # ProxyAgent is appended below with its real semantics; a Router-made
        # one would silently replace the clarification channel.
        if lowered in seen or lowered == "proxyagent":
            continue
        coding_tools = bool(raw.get("coding_tools"))
        use_bing = bool(raw.get("use_bing"))
        # The factory raises on reasoning combined with bing/coding tools;
        # keep the concrete, verifiable capabilities (files, live web) over
        # "reasoning harder".
        use_reasoning = (
            bool(raw.get("use_reasoning")) and not coding_tools and not use_bing
        )
        seen.add(lowered)
        agents.append(
            {
                "input_key": "",
                "type": "",
                "name": name,
                "deployment_name": deployment,
                "icon": "",
                "system_message": str(raw.get("system_message") or "").strip(),
                "description": str(raw.get("description") or "").strip(),
                # No composed team has a Search index; use_rag without
                # index_name yields SearchConfig=None in the factory — an agent
                # that believes it has a knowledge base and does not.
                "use_rag": False,
                "use_mcp": bool(raw.get("use_mcp")),
                "use_bing": use_bing,
                "use_reasoning": use_reasoning,
                "index_name": "",
                "coding_tools": coding_tools,
            }
        )
        if len(agents) >= _COMPOSED_TEAM_MAX_AGENTS:
            break

    if not agents:
        raise ValueError("router roster contained no usable agents")

    agents.append(
        {
            "input_key": "",
            "type": "",
            "name": "ProxyAgent",
            "deployment_name": "",
            "icon": "",
            "system_message": "",
            "description": "",
            "use_rag": False,
            "use_mcp": False,
            "use_bing": False,
            "use_reasoning": False,
            "index_name": "",
            "coding_tools": False,
        }
    )

    team_service = TeamService(memory_store)
    team = await team_service.validate_and_parse_team_config(
        {
            "name": "Auto Team",
            # hidden: composed per request — not an entry in the UI picker
            # (_merge_teams_for_direct_response skips hidden the same way).
            "status": "hidden",
            "deployment_name": deployment,
            "description": (
                f"Team composed by the Model Router for: {description[:200]}"
            ),
            "agents": agents,
            "starting_tasks": [
                {
                    "id": "task-1",
                    "name": "Requested task",
                    "prompt": description[:500],
                    "created": "",
                    "creator": "",
                    "logo": "",
                }
            ],
        },
        user_id,
    )
    await team_service.save_team_configuration(team)
    logger.info(
        "Composed team '%s' (%s) from router roster: %s",
        team.name,
        team.team_id,
        [
            f"{a.name}(code={a.coding_tools},mcp={a.use_mcp},bing={a.use_bing},reason={a.use_reasoning})"
            for a in team.agents
        ],
    )
    return team


async def _create_plan_and_start(
    *,
    background_tasks: BackgroundTasks,
    user_id: str,
    tenant_id: str,
    user_access_token: Optional[str],
    description: str,
    session_id: str,
    history: Optional[list] = None,
    persist_user_task: bool = False,
    composed_agents: Optional[list] = None,
    workspace_id: Optional[str] = None,
) -> str:
    """Create a Plan and kick off the Magentic orchestration as a BackgroundTask.

    Shared core of ``POST /process_request`` and the chat ``run_plan`` escalation
    (the Model Router routing a turn to the formal multi-agent Plan). Returns the
    new ``plan_id``; the orchestration runs after return and streams to PlanPage
    over WebSocket. Raises HTTPException (404 no team / 400 RAI / 500 create).

    ``composed_agents`` is the roster the Model Router proposed in the same
    ``run_plan`` call that escalated the turn. When present and usable, the
    team is composed from it (sanitized + persisted) instead of requiring a
    manually selected team — the roster must exist BEFORE the Magentic graph
    is built, because the graph freezes its participants at ``build()``.
    """
    try:
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team: Optional[TeamConfiguration] = None
        if composed_agents:
            try:
                team = await _team_from_router_roster(
                    composed_agents, description, user_id, memory_store
                )
            except Exception as compose_err:
                # A malformed roster must never block the request — fall back
                # to the user's selected team (the pre-existing behavior).
                logger.warning(
                    "Router roster rejected (%s); using the selected team",
                    compose_err,
                )
        if team is None:
            user_current_team = await memory_store.get_current_team(user_id=user_id)
            selected_team_id: str | None = (
                user_current_team.team_id if user_current_team else None
            )
            if not selected_team_id:
                raise HTTPException(
                    status_code=404,
                    detail="No team configured. Please select a team first.",
                )
            team = await memory_store.get_team_by_id(team_id=selected_team_id)
            if not team:
                raise HTTPException(
                    status_code=404,
                    detail=f"Team configuration '{selected_team_id}' not found or access denied",
                )
        team_id: str | None = team.team_id
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error retrieving team configuration: {e}",
        ) from e

    if not await rai_success(description, team, memory_store):
        track_event_if_configured(
            "Error_RAI_Check_Failed",
            {
                "status": "Plan not created - RAI check failed",
                "description": description,
                "session_id": session_id,
            },
        )
        raise HTTPException(
            status_code=400,
            detail="Request contains content that doesn't meet our safety guidelines, try again.",
        )

    # Recover the SAME session context the router uses (single source of
    # truth). The SSE run_plan escalation passes the already-recovered history;
    # process_request (no prior recovery) recovers it here via the same helper
    # — one loader, not two parallel ones. It rides to run_orchestration and
    # enters the Magentic manager as MagenticContext.chat_history (Messages),
    # never welded into the task string.
    if history is None:
        _ctx_svc = await get_chat_cosmos_service()
        history = await _recover_session_context(
            _ctx_svc, session_id, user_id, description
        )
    input_task = InputTask(
        session_id=session_id,
        description=description,
        workspace_id=workspace_id,
    )

    try:
        plan_id = str(uuid.uuid4())

        if persist_user_task:
            # The plan result IS written back to this session, so without the
            # originating request the history reads as orphan reports with no
            # questions — indistinguishable from duplication. The chat lane
            # already persists the user message before escalating; the direct
            # lane (/process_request) had no writer at all.
            # Written BEFORE the plan document so its timestamp precedes the
            # plan's: the UI orders the canvas by that boundary, and a request
            # must never render below the plan it produced.
            try:
                _chat_svc_q = await get_chat_cosmos_service()
                await _chat_svc_q.add_message(
                    session_id=session_id,
                    user_id=user_id,
                    content=description,
                    role="user",
                    metadata={"intent": "task", "plan_id": plan_id},
                )
            except Exception as _qe:
                logger.warning("Could not persist plan request to chat_cosmos: %s", _qe)

        # Initialize memory store and service
        plan = Plan(
            id=plan_id,
            plan_id=plan_id,
            user_id=user_id,
            session_id=session_id,
            team_id=team_id,
            initial_goal=description,
            overall_status=PlanStatus.in_progress,
        )
        await memory_store.add_plan(plan)

        try:
            _chat_svc = await get_chat_cosmos_service()
            await _chat_svc.add_message(
                session_id=session_id,
                user_id=user_id,
                content="",
                role="assistant",
                metadata={"intent": "task", "plan_id": plan_id},
            )
        except Exception as _e:
            logger.warning("Could not write task anchor to chat_cosmos: %s", _e)

        # Ensure orchestration is initialized before running
        # Force rebuild for each new task since Magentic workflows cannot be reused after completion
        team_service = TeamService(memory_store)
        await OrchestrationManager.get_current_or_new_orchestration(
            user_id=user_id,
            team_config=team,
            team_switched=False,
            team_service=team_service,
            force_rebuild=True,  # Always rebuild workflow for new tasks
            # OBO: agents built here run in a BackgroundTask under the app MI unless
            # they carry the user's assertion. Thread it so orchestration agents
            # authenticate as the user (same as direct chat) — required for
            # user-delegated tool connections (e.g. WorkIQ/agent365).
            user_access_token=user_access_token,
        )

        track_event_if_configured(
            "Plan_Created",
            {
                "status": "success",
                "plan_id": plan.plan_id,
                "session_id": session_id,
                "user_id": user_id,
                "team_id": team_id,
                "description": description,
            },
        )
    except Exception as e:
        print(f"Error creating plan: {e}")
        track_event_if_configured(
            "Error_Plan_Creation_Failed",
            {
                "status": "error",
                "description": description,
                "session_id": session_id,
                "user_id": user_id,
                "error": str(e),
            },
        )
        raise HTTPException(status_code=500, detail="Failed to create plan") from e

    try:

        async def run_orchestration_task():
            try:
                await OrchestrationManager().run_orchestration(
                    user_id,
                    session_id,
                    input_task,
                    plan_id=plan_id,
                    history=history,
                    workspace_id=workspace_id,
                )
            finally:
                orchestration_config.clear_run_active(session_id)

        # Mark the session's run in flight BEFORE returning, so a near-immediate
        # resume_plan from PlanPage (orphan recovery on a freshly-created plan)
        # is skipped instead of starting a duplicate orchestration.
        orchestration_config.mark_run_active(session_id)
        background_tasks.add_task(run_orchestration_task)
        return plan_id

    except Exception as e:
        track_event_if_configured(
            "Error_Request_Start_Failed",
            {
                "session_id": session_id,
                "description": description,
                "error": str(e),
            },
        )
        raise HTTPException(
            status_code=400, detail=f"Error starting request: {e}"
        ) from e


# ── Session-aware intent helper ──────────────────────────────────────


async def _get_previous_intent(
    chat_svc: Any,
    session_id: str,
    user_id: str,
) -> Optional[str]:
    """Return the intent of the last assistant message in this session.

    Reads exclusively from chat_cosmos — single source of truth.
    The task anchor is written by process_request at plan-creation time,
    so this function never needs to query any other store.
    """
    try:
        session = await chat_svc.get_session(session_id, user_id)
        if session and session.get("messages"):
            for msg in reversed(session["messages"]):
                if msg.get("role") == "assistant":
                    intent = (msg.get("metadata") or {}).get("intent")
                    if intent:
                        return intent
    except Exception:
        pass
    return None


async def _recover_session_context(
    chat_svc: Any,
    session_id: str,
    user_id: str,
    current_message: str,
) -> list:
    """Rebuild conversation memory from the SINGLE source of truth (Cosmos + AI
    Search) — the SAME recovery the direct-chat path uses, so Plan and chat share
    one context, not two parallel loaders. Two layers, deduped, oldest→newest:
      long memory  → hybrid keyword+vector+semantic retrieval across ALL of the
                     user's history (search_chat_history);
      short memory → this session's turns in order (conversational continuity).
    The current user message is skipped (already persisted by the caller).
    """
    history: list = []
    try:
        seen: set = set()
        cur = (current_message or "").strip()
        from common.services.search_index_service import get_search_index_service

        search_svc = await get_search_index_service()
        hits = await search_svc.search_chat_history(
            query=current_message,
            user_id=user_id,
            top_k=15,
        )
        for h in sorted(hits, key=lambda x: x.get("timestamp", "")):
            c = (h.get("content") or "").strip()
            if c and c != cur and c not in seen:
                seen.add(c)
                history.append({"role": h.get("role", "user"), "content": c})
        session = await chat_svc.get_session(session_id, user_id)
        for m in (session or {}).get("messages", []):
            c = (m.get("content") or "").strip()
            if c and c != cur and c not in seen:
                seen.add(c)
                history.append({"role": m.get("role", "user"), "content": c})
    except Exception as _hist_err:
        logger.warning("Could not rebuild chat history: %s", _hist_err)
    return history


# ── Chat Mode Endpoint (P0 — conversational without plan) ────────────


@app_v4.post("/chat/upload-file")
async def chat_upload_file(
    request: Request,
    file: UploadFile = File(...),
):
    """
    Upload a file to Azure AI Foundry for use with code_interpreter.

    Returns a file_id to include in the subsequent chat/message/stream request
    via the file_ids field. Foundry attaches it to the thread message, making
    it available for code_interpreter to read and process.

    ---
    tags:
      - Chat
    """
    from azure.ai.agents.aio import AgentsClient

    from common.config.app_config import config as app_config

    _extract_auth_with_token(request)
    # Borrowed process-scoped credential (closed once in the lifespan).
    # Minting one per request leaked its aiohttp ClientSession: async
    # credentials own a session and nothing here ever closed it.
    creds = app_config.get_shared_async_credential()

    contents = await file.read()
    filename = file.filename or "upload"

    # No size judgement here: this endpoint is transport. Whether the file has
    # 0 bytes or 3000 is content, and reading content is the code interpreter's
    # job — rejecting an empty upload also denied the model the chance to say
    # "this file is empty", which is an answer the user asked for.
    logger.info(
        "Uploading file to Foundry: name=%s size=%d bytes content_type=%s",
        filename,
        len(contents),
        file.content_type,
    )

    try:
        async with AgentsClient(
            endpoint=app_config.AZURE_AI_PROJECT_ENDPOINT,
            credential=creds,
        ) as agents_client:
            # Pass a (filename, bytes, content_type) tuple — same pattern used by
            # setup_search_pipeline.py: send raw bytes with explicit filename so
            # Foundry can identify the file and avoids "File is empty" errors.
            mime = file.content_type or "application/octet-stream"
            uploaded = await agents_client.files.upload(
                file=(filename, contents, mime),
                purpose="assistants",
            )

            logger.info(
                "File uploaded to Foundry: file_id=%s name=%s size=%d bytes",
                uploaded.id,
                filename,
                len(contents),
            )
            return {"file_id": uploaded.id, "filename": filename, "size": len(contents)}
    except Exception as ex:
        logger.error("File upload to Foundry failed: %s", ex)
        raise HTTPException(status_code=500, detail=f"File upload failed: {ex}")


# ── Generated-file persistence plumbing ──────────────────────────────
# Fire-and-forget with a strong reference (unreferenced tasks can be GC'd
# mid-flight) and exception retrieval (else asyncio logs "exception was
# never retrieved" at teardown).
_BG_PERSIST_TASKS: set = set()


def _spawn_bg_persist(coro, label: str) -> None:
    task = asyncio.create_task(coro)
    _BG_PERSIST_TASKS.add(task)

    def _done(t) -> None:
        _BG_PERSIST_TASKS.discard(t)
        exc = None if t.cancelled() else t.exception()
        if exc:
            logger.error("Background persist %s failed: %s", label, exc)

    task.add_done_callback(_done)


def _file_response(data: bytes, filename: str):
    """Bytes → streaming-friendly file Response with inferred content type."""
    import mimetypes

    from fastapi.responses import Response

    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"
    previewable_prefixes = ("image/", "text/")
    previewable_mimes = {"application/pdf"}
    disposition = (
        "inline"
        if mime in previewable_mimes or mime.startswith(previewable_prefixes)
        else "attachment"
    )
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Content-Length": str(len(data)),
            "Cache-Control": "private, max-age=86400, immutable",
        },
    )


class HtmlPreviewRequest(BaseModel):
    """Model-generated HTML to publish as an isolated-origin preview."""

    html: str
    title: str = ""


@app_v4.post("/chat/preview")
async def create_html_preview(request: Request, body: HtmlPreviewRequest):
    """Publish model-generated HTML on the Blob origin and return its SAS URL.

    The preview iframe needs a REAL origin (location/history/hash routing and
    storage all dead in an opaque srcdoc origin). The storage account is that
    origin: distinct from frontend and backend in dev and prod, no cookies or
    ambient credentials — ``allow-same-origin`` there never means MACAE.
    """
    user_id, _tenant_id = _extract_auth(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing user identity")
    if len(body.html) > 2_000_000:
        raise HTTPException(status_code=413, detail="Preview HTML too large")

    from v4.common.services.generated_file_store import GeneratedFileStore

    blob_name = f"preview_{uuid.uuid4().hex}.html"
    url = await GeneratedFileStore.get_instance().save_preview_html(
        blob_name, body.html
    )
    if not url:
        raise HTTPException(status_code=502, detail="Preview publish failed")
    return {"url": url}


@app_v4.get("/chat/download-file/{file_id}")
async def chat_download_file(
    request: Request,
    file_id: str,
    container_id: str | None = None,
):
    """
    Download a file generated by code_interpreter (Assistants API).

    The file_id comes from annotations[].file_id emitted in the SSE stream
    as a ``generated_file`` event. The file bytes are retrieved from Foundry
    using AgentsClient and streamed back to the caller.

    ---
    tags:
      - Chat
    """
    from common.config.app_config import config as app_config
    from v4.common.services.generated_file_store import GeneratedFileStore

    # Blob first: files persisted at generation time outlive their Foundry
    # container. A miss means the file predates the store → live-Foundry path.
    stored = await GeneratedFileStore.get_instance().load(file_id)
    if stored is not None:
        data, filename = stored
        return _file_response(data, filename)

    try:
        if file_id.startswith("cfile_"):
            if not container_id:
                raise HTTPException(
                    status_code=400,
                    detail="container_id is required to download generated container files",
                )

            # The container was created by the chat's direct Responses API call
            # ({account}/openai, api-version 2025-03-01-preview). It must be read
            # back through the SAME endpoint + identity: the project client
            # (get_openai_client) targets a different scope and 404s ("Container
            # not found"), and a user-scoped container is invisible to a
            # different identity — so mirror _RouterChatClient's OBO bearer.
            from openai import AsyncOpenAI, NotFoundError

            access_token = None
            if request is not None:
                try:
                    _, _, access_token = _extract_auth_with_token(request)
                except Exception:
                    access_token = None
            _obo_cred = None
            try:
                if access_token and app_config.ENABLE_OBO:
                    _obo_cred = app_config.build_user_credential(access_token)
                    bearer = (
                        await _obo_cred.get_token("https://ai.azure.com/.default")
                    ).token
                else:
                    bearer = (
                        await app_config.get_shared_async_credential().get_token(
                            "https://ai.azure.com/.default"
                        )
                    ).token
                account = (app_config.AZURE_AI_PROJECT_ENDPOINT or "").split(
                    "/api/projects/"
                )[0]
                openai = AsyncOpenAI(
                    api_key=bearer,
                    base_url=f"{account}/openai",
                    default_query={"api-version": _DIRECT_RESPONSES_API_VERSION},
                    timeout=60,
                )
                try:
                    file_info: Any = await openai.containers.files.retrieve(
                        file_id=file_id,
                        container_id=container_id,
                    )
                    file_path = getattr(file_info, "path", None) or file_id
                    filename = (
                        os.path.basename(file_path) if file_path != file_id else file_id
                    )
                    content = await openai.containers.files.content.retrieve(
                        file_id=file_id,
                        container_id=container_id,
                    )
                    data = await content.aread()
                except NotFoundError as nf:
                    # Container recycled (~20 min) and the file was never
                    # persisted (predates the store). Terminal state: the
                    # bytes no longer exist anywhere.
                    raise HTTPException(
                        status_code=410,
                        detail=(
                            "Generated file expired with its Foundry container "
                            "before persistence existed"
                        ),
                    ) from nf
                finally:
                    await openai.close()
            finally:
                if _obo_cred is not None:
                    try:
                        await _obo_cred.close()
                    except Exception:
                        pass
        else:
            from azure.ai.agents.aio import AgentsClient

            # Borrowed shared credential — per-request minting leaked the
            # credential's aiohttp ClientSession (nothing closed it).
            creds = app_config.get_shared_async_credential()
            async with AgentsClient(
                endpoint=app_config.AZURE_AI_PROJECT_ENDPOINT,
                credential=creds,
            ) as agents_client:
                file_info = await agents_client.files.get(file_id)
                filename = getattr(file_info, "filename", None) or file_id

                content_stream = await agents_client.files.get_content(file_id)
                chunks = []
                async for chunk in content_stream:
                    chunks.append(bytes(chunk))
                data = b"".join(chunks)

        # Backfill: a pre-store file just fetched from live Foundry gets
        # persisted so the NEXT read outlives the container.
        _spawn_bg_persist(
            GeneratedFileStore.get_instance().save(file_id, filename, data),
            f"backfill:{file_id}",
        )
        return _file_response(data, filename)
    except HTTPException:
        raise  # preserve the original status code (e.g. 400 for missing container_id)
    except ResourceNotFoundError as nf:
        # The AgentsClient branch above raises azure-core's ResourceNotFoundError,
        # not openai's NotFoundError, so it never hit the 410 handler and fell
        # through to the generic 500 below — a missing file reported as a server
        # fault. 404 is the truth here, and the schema already declares it.
        logger.info("Generated file not found in Foundry: file_id=%s", file_id)
        raise HTTPException(
            status_code=404, detail=f"Generated file '{file_id}' not found"
        ) from nf
    except Exception as ex:
        logger.error(
            "File download from Foundry failed: file_id=%s error=%s", file_id, ex
        )
        raise HTTPException(status_code=500, detail=f"File download failed: {ex}")


@app_v4.post("/chat/message")
async def chat_message(
    background_tasks: BackgroundTasks,
    chat_request: ChatMessageRequest,
    request: Request,
):
    """
    Handle a chat message with intent classification.

    Routes messages to the appropriate handler:
    - "task" → Redirects to process_request (full plan workflow)
    - "conversational" → Direct agent response without plan creation
    - "mcp_query" → MCP Inspector / bridge query

    ---
    tags:
      - Chat
    """
    from v4.orchestration.intent_router import Intent, IntentRouter

    user_id, tenant_id, user_access_token = _extract_auth_with_token(request)

    # Assign session_id if not provided
    if not chat_request.session_id:
        chat_request.session_id = str(uuid.uuid4())

    # ── Persist user message to Cosmos DB ────────────────────────
    chat_svc = await get_chat_cosmos_service()
    try:
        await chat_svc.add_message(
            session_id=chat_request.session_id,
            user_id=user_id,
            content=chat_request.message,
            role="user",
        )
    except Exception as e:
        logger.warning("Could not persist user chat message: %s", e)

    previous_intent = await _get_previous_intent(
        chat_svc, chat_request.session_id, user_id
    )

    # Front-door decision first. Do not invoke a worker agent before deciding
    # whether this request should create a formal plan.
    intent_result = await IntentRouter.classify_async(
        chat_request.message,
        previous_intent=previous_intent,
    )
    logger.info(
        "Chat intent: %s (confidence=%.2f, prev=%s) for message: %s",
        intent_result.intent.value,
        intent_result.confidence,
        previous_intent,
        chat_request.message[:80],
    )

    # Never create a new plan when the message originates from an open plan OR
    # the UI selector is in Chat position (allow_plan=False).
    if (
        chat_request.plan_id or not chat_request.allow_plan
    ) and intent_result.intent == Intent.TASK:
        logger.info(
            "plan_id=%s allow_plan=%s — downgrading TASK to CONVERSATIONAL",
            chat_request.plan_id,
            chat_request.allow_plan,
        )
        from v4.orchestration.intent_router import IntentResult

        intent_result = IntentResult(
            intent=Intent.CONVERSATIONAL,
            confidence=intent_result.confidence,
            reasoning="in-plan follow-up or chat-only selector — not a new task",
        )

    # ── Route by intent ──────────────────────────────────────────
    if intent_result.intent == Intent.TASK:
        input_task_for_plan = InputTask(
            session_id=chat_request.session_id,
            description=chat_request.message,
            workspace_id=chat_request.workspace_id,
        )
        try:
            result = await process_request(
                background_tasks, input_task_for_plan, request
            )
            # process_request already wrote the task anchor to chat_cosmos.
            return ChatMessageResponse(
                session_id=chat_request.session_id,
                intent="task",
                confidence=intent_result.confidence,
                response="I've created a plan for your request. Redirecting to plan view.",
                agent="planner",
                redirect_to_plan=result.get("plan_id"),
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Error creating plan from chat: %s", e)
            raise HTTPException(
                status_code=500, detail=f"Error creating plan: {e}"
            ) from e

    else:
        actual_intent = intent_result.intent.value  # "mcp_query" or "conversational"
        agent_response = await _get_mcp_query_response(
            chat_request.message,
            chat_request.session_id,
            user_id,
            chat_svc,
            tenant_id=tenant_id,
            user_access_token=user_access_token,
        )
        response_text = agent_response

        # Persist assistant response with the precise intent label
        try:
            await chat_svc.add_message(
                session_id=chat_request.session_id,
                user_id=user_id,
                content=response_text,
                role="assistant",
                metadata={"intent": actual_intent},
            )
        except Exception as e:
            logger.warning("Could not persist %s response: %s", actual_intent, e)

        track_event_if_configured(
            f"Chat_{actual_intent}",
            {
                "session_id": chat_request.session_id,
                "user_id": user_id,
                "message": chat_request.message[:200],
            },
        )

        return ChatMessageResponse(
            session_id=chat_request.session_id,
            intent=actual_intent,
            confidence=intent_result.confidence,
            response=response_text,
            agent="assistant",
        )


# ── Streaming Chat Endpoint (SSE) ────────────────────────────────


def _sse_event(data: dict) -> str:
    """Format a dict as an SSE data event."""
    return f"data: {json.dumps(data)}\n\n"


def _safe_json_dumps(value: Any) -> str:
    """Safely serialize arbitrary SDK objects for logs/SSE previews."""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _to_safe_dict(value: Any, max_depth: int = 4, _depth: int = 0) -> Any:
    """Best-effort conversion of SDK event objects to JSON-safe structures."""
    if _depth >= max_depth:
        return str(value)

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {
            str(k): _to_safe_dict(v, max_depth=max_depth, _depth=_depth + 1)
            for k, v in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [_to_safe_dict(v, max_depth=max_depth, _depth=_depth + 1) for v in value]

    # Pydantic-like models
    for attr in ("model_dump", "dict"):
        meth = getattr(value, attr, None)
        if callable(meth):
            try:
                dumped = meth()
                return _to_safe_dict(dumped, max_depth=max_depth, _depth=_depth + 1)
            except Exception:
                pass

    # Generic object attributes
    try:
        if hasattr(value, "__dict__"):
            return _to_safe_dict(
                {
                    k: v
                    for k, v in vars(value).items()
                    if not str(k).startswith("_") and not callable(v)
                },
                max_depth=max_depth,
                _depth=_depth + 1,
            )
    except Exception:
        pass

    return str(value)


def _extract_function_result_payload(item: Any) -> dict[str, Any]:
    """Extract rich tool-result payload from response output items."""
    safe_item = _to_safe_dict(item)
    payload: dict[str, Any] = {
        "type": getattr(item, "type", None),
        "call_id": getattr(item, "call_id", None),
        "status": getattr(item, "status", None),
        "name": getattr(item, "name", None),
        "arguments": getattr(item, "arguments", None),
    }

    # Common payload-bearing fields across SDK/event variants
    for field in (
        "output",
        "result",
        "content",
        "text",
        "value",
        "message",
        "stdout",
        "stderr",
    ):
        val = getattr(item, field, None)
        if val not in (None, ""):
            payload[field] = _to_safe_dict(val)

    # Include the raw normalized snapshot for forward compatibility/debugging
    payload["raw"] = safe_item
    return payload


def _item_payload_str(item: Any, *fields: str) -> str:
    """Return first non-empty string-ish payload from candidate fields."""
    for field in fields:
        val = getattr(item, field, None)
        if val not in (None, ""):
            if isinstance(val, str):
                return val
            return _safe_json_dumps(_to_safe_dict(val))
    return ""


def _extract_annotations(item: Any) -> list[dict]:
    """Normalize annotations from output item into list[dict]."""
    anns = getattr(item, "annotations", None) or []
    out: list[dict] = []
    for ann in anns:
        if isinstance(ann, dict):
            out.append(ann)
        else:
            out.append(
                _to_safe_dict(ann) if isinstance(_to_safe_dict(ann), dict) else {}
            )
    return out


def _build_hosted_file_from_annotation(ann: dict) -> Optional[Any]:
    file_id = ann.get("file_id")
    if not file_id:
        return None
    add_props = ann.get("additional_properties")
    if not isinstance(add_props, dict):
        add_props = {}
    name = ann.get("url") or add_props.get("filename") or ann.get("text") or file_id
    return _HostedHostedFile(
        file_id=file_id,
        name=name,
        additional_properties=add_props,
    )


def _is_invokable_agent(agent: Any) -> bool:
    return callable(getattr(agent, "invoke", None))


def _agent_runtime_name(agent: Any) -> str:
    return getattr(agent, "agent_name", None) or getattr(agent, "name", "") or ""


def _agent_config_by_name(team: Any) -> dict[str, Any]:
    return {
        (getattr(agent, "name", "") or "").lower(): agent
        for agent in getattr(team, "agents", []) or []
        if getattr(agent, "name", "")
    }


def _build_agent_description(agent: Any, config: Any) -> str:
    """Build a rich description for an agent, including its capabilities."""
    description = getattr(config, "description", "") or ""
    capabilities = []
    if getattr(config, "use_rag", False):
        index_name = getattr(config, "index_name", "") or ""
        capabilities.append(
            f"RAG/Search{f'(index={index_name})' if index_name else ''}"
        )
    if getattr(config, "use_mcp", False):
        capabilities.append("MCP/external-tools")
    if getattr(config, "use_reasoning", False):
        capabilities.append("Reasoning")
    if getattr(config, "coding_tools", False):
        capabilities.append("CodeInterpreter")
    if capabilities:
        description = f"{description} [capabilities: {', '.join(capabilities)}]".strip()
    return description or "(no description)"


def _match_agent_by_name(chosen: str, agents: list[Any]) -> Optional[Any]:
    """Fuzzy-match LLM response to an agent.

    Tries (in order):
    1. Exact case-insensitive match on the full name.
    2. The chosen string is contained in the agent name (handles trailing punctuation / spacing).
    3. The agent name is contained in the chosen string (handles extra explanation text).
    Returns None if no match found.
    """
    chosen_stripped = chosen.strip().rstrip(".,;:!?").lower()
    for a in agents:
        name = (_agent_runtime_name(a) or "").lower()
        if name == chosen_stripped:
            return a
    for a in agents:
        name = (_agent_runtime_name(a) or "").lower()
        if chosen_stripped in name or name in chosen_stripped:
            return a
    return None


async def _select_team_agent(message: str, team: Any, agents: list[Any]) -> Any:
    """Select the best agent using ProxyAgent as the primary intermediary.

    ProxyAgent acts as the central coordinator between the user and all
    specialized agents. It receives the user request, understands the context,
    and delegates tasks to the appropriate specialist agents (TechnicalSupportAgent,
    HRHelperAgent, MarketingAgent, etc.) as needed.

    Selection strategy:
    1. If a ProxyAgent is available, always prefer it — it is the designated
       intermediary that maintains conversation context and routes internally.
    2. If there is only one invokable agent, return it directly.
    3. Fall back to LLM-based routing only when no ProxyAgent is present and
       multiple specialist agents are available.
    """
    from common.config.app_config import config as app_config

    invokable_agents = [agent for agent in agents if _is_invokable_agent(agent)]
    if not invokable_agents:
        return None

    if len(invokable_agents) == 1:
        return invokable_agents[0]

    # Prefer ProxyAgent as the central intermediary/orchestrator
    for agent in invokable_agents:
        name = (_agent_runtime_name(agent) or "").lower()
        if name == "proxyagent":
            logger.info(
                "Routing message through ProxyAgent (central intermediary) for team '%s'",
                getattr(team, "name", "unknown"),
            )
            return agent

    # No ProxyAgent found — fall back to LLM-based routing among specialist agents
    logger.info(
        "No ProxyAgent found in team '%s'; using LLM router across %d agents.",
        getattr(team, "name", "unknown"),
        len(invokable_agents),
    )

    configs = _agent_config_by_name(team)
    agent_lines = []
    for a in invokable_agents:
        name = _agent_runtime_name(a) or ""
        cfg = configs.get(name.lower())
        desc = _build_agent_description(a, cfg) if cfg else "(no description)"
        agent_lines.append(f"- {name}: {desc}")
    agent_list = "\n".join(agent_lines)

    prompt = (
        "You are an agent router. Given the list of agents and a user message, "
        "reply with ONLY the exact name of the single agent that best handles the request. "
        "No explanation, no punctuation — just the agent name.\n\n"
        f"Agents:\n{agent_list}\n\n"
        f"User message: {message}\n\n"
        "Agent name:"
    )

    chosen_agent: Optional[Any] = None
    try:
        project = app_config.get_ai_project_client()
        try:
            openai = project.get_openai_client()
            try:
                resp = await openai.chat.completions.create(
                    model=app_config.AZURE_OPENAI_DEPLOYMENT_NAME,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=30,
                    temperature=0,
                )
                chosen_text = (resp.choices[0].message.content or "").strip()
                logger.info("LLM router raw response: %r", chosen_text)
                chosen_agent = _match_agent_by_name(chosen_text, invokable_agents)
                if chosen_agent is None:
                    logger.warning(
                        "LLM router returned unrecognised agent name %r; "
                        "falling back to first agent. Available: %s",
                        chosen_text,
                        [_agent_runtime_name(a) for a in invokable_agents],
                    )
            finally:
                await openai.close()
        finally:
            if hasattr(project, "close"):
                await project.close()
    except Exception as exc:
        logger.warning("LLM router failed (%s); falling back to first agent.", exc)

    return chosen_agent or invokable_agents[0]


def _team_agent_context(team: Any) -> str:
    lines = []
    team_name = getattr(team, "name", "") or "Current team"
    team_description = getattr(team, "description", "") or ""
    lines.append(f"Current team: {team_name}")
    if team_description:
        lines.append(f"Team description: {team_description}")
    lines.append("Available team agents:")
    for agent in getattr(team, "agents", []) or []:
        name = getattr(agent, "name", "") or "UnnamedAgent"
        description = getattr(agent, "description", "") or ""
        capabilities = []
        if getattr(agent, "use_rag", False):
            index_name = getattr(agent, "index_name", "") or ""
            capabilities.append(
                f"RAG/Search{f' index={index_name}' if index_name else ''}"
            )
        if getattr(agent, "use_reasoning", False):
            capabilities.append("Reasoning")
        if getattr(agent, "use_mcp", False):
            capabilities.append("MCP")
        if getattr(agent, "coding_tools", False):
            capabilities.append("Code Interpreter")
        suffix = f" ({', '.join(capabilities)})" if capabilities else ""
        lines.append(f"- {name}{suffix}: {description}")
    return "\n".join(lines)


def _build_direct_chat_prompt(message: str, team: Any, selected_agent: Any) -> str:
    selected_name = _agent_runtime_name(selected_agent) or "UnknownAgent"
    return (
        "ROUTING CONTEXT FROM MACAE BACKEND\n"
        f"Selected responding agent for this turn: {selected_name}\n"
        f"{_team_agent_context(team)}\n\n"
        "Instructions:\n"
        "- If the user asks which agent is responding, answer with the selected "
        "responding agent name above.\n"
        "- Do not say you lack access to the current agent/team when the routing "
        "context provides it.\n"
        "- For domain questions, use your configured tools/knowledge as usual.\n"
        "- Respond in the user's language.\n\n"
        f"USER MESSAGE:\n{message}"
    )


def _get_m_plan_id_from_plan(plan: Any) -> Optional[str]:
    m_plan = getattr(plan, "m_plan", None)
    if isinstance(m_plan, dict):
        return m_plan.get("id") or m_plan.get("m_plan_id")
    return getattr(m_plan, "id", None) or getattr(m_plan, "m_plan_id", None)


def _build_plan_chat_prompt(
    message: str,
    team: Any,
    selected_agent: Any,
    plan: Any,
) -> str:
    selected_name = _agent_runtime_name(selected_agent) or "UnknownAgent"
    m_plan = getattr(plan, "m_plan", None)
    if m_plan and hasattr(m_plan, "model_dump"):
        m_plan = m_plan.model_dump()

    return (
        "ACTIVE PLAN CONTEXT FROM MACAE BACKEND\n"
        f"plan_id: {getattr(plan, 'plan_id', '') or getattr(plan, 'id', '')}\n"
        f"m_plan_id: {_get_m_plan_id_from_plan(plan) or ''}\n"
        f"plan_status: {getattr(plan, 'overall_status', '')}\n"
        f"initial_goal: {getattr(plan, 'initial_goal', '')}\n"
        f"Selected responding agent for this turn: {selected_name}\n"
        f"{_team_agent_context(team)}\n\n"
        "Current m_plan object, if available:\n"
        f"{json.dumps(m_plan, ensure_ascii=False, default=str) if m_plan else '{}'}\n\n"
        "Instructions:\n"
        "- This message is inside the active application Plan context above.\n"
        "- Do not create a new application Plan for this message.\n"
        "- Use the active plan state and team as authoritative context.\n"
        "- If the user is asking about status, next steps, approval, or prior work, "
        "answer in relation to the active plan.\n"
        "- Respond in the user's language.\n\n"
        f"USER MESSAGE:\n{message}"
    )


def _agent_can_join_direct_orchestration(agent: Any) -> bool:
    """Return True for agents that implement the AgentFramework protocol."""
    return callable(getattr(agent, "run", None)) or callable(
        getattr(agent, "invoke", None)
    )


def _merge_teams_for_direct_response(
    teams: list[TeamConfiguration],
    user_id: str,
    default_deployment_name: str,
) -> TeamConfiguration:
    """Build a direct-response team from all available teams.

    ProxyAgent is intentionally kept once with its original clarification role.
    Business agents keep their names so the Magentic manager can route to the
    same participants users see elsewhere in the app.
    """
    merged_agents: list[TeamAgent] = []
    seen_names: set[str] = set()
    descriptions: list[str] = []

    for team in teams:
        if getattr(team, "status", "visible") == "hidden":
            continue
        team_name = getattr(team, "name", "") or "Unnamed Team"
        team_description = getattr(team, "description", "") or ""
        descriptions.append(f"{team_name}: {team_description}".strip())

        for agent in getattr(team, "agents", []) or []:
            name = getattr(agent, "name", "") or ""
            if not name:
                continue
            normalized = name.lower()
            if normalized == "proxyagent":
                if "proxyagent" in seen_names:
                    continue
                seen_names.add("proxyagent")
                merged_agents.append(agent)
                continue
            if normalized in seen_names:
                logger.warning(
                    "Skipping duplicate direct-response agent name '%s' from team '%s'",
                    name,
                    team_name,
                )
                continue
            seen_names.add(normalized)

            agent_dict = (
                agent.model_dump() if hasattr(agent, "model_dump") else dict(agent)
            )
            agent_dict["description"] = (
                f"[Team: {team_name}] {agent_dict.get('description', '')}".strip()
            )
            agent_dict["system_message"] = (
                f"Team context: {team_name}. {team_description}\n\n"
                f"{agent_dict.get('system_message', '')}"
            ).strip()
            merged_agents.append(TeamAgent(**agent_dict))

    if not any((agent.name or "").lower() == "proxyagent" for agent in merged_agents):
        merged_agents.append(
            TeamAgent(
                input_key="",
                type="",
                name="ProxyAgent",
                deployment_name=default_deployment_name,
                icon="",
                system_message="",
                description="Clarification agent for missing user details.",
            )
        )

    return TeamConfiguration(
        team_id="direct-response-team",
        name="Direct Response Team",
        status="visible",
        created="",
        created_by=user_id,
        deployment_name=default_deployment_name,
        agents=merged_agents,
        description=(
            "Direct response orchestration across all available teams. "
            + "\n".join(descriptions)
        ),
        logo="",
        plan="",
        starting_tasks=[],
        user_id=user_id,
    )


class _HostedTextContent:
    """Content shim mimicking an agent_framework text content (.type / .text)."""

    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _HostedPlanSignal:
    """Content shim signalling the Model Router escalated this turn to the formal
    multi-agent Plan (``run_plan`` capability). The SSE handler turns it into plan
    creation + a ``plan_created`` event; it is never streamed as text."""

    type = "plan_signal"

    def __init__(self, task: str, agents: Optional[list] = None) -> None:
        self.task = task
        # Roster the Router proposed in the same run_plan call (None → the
        # user's selected team). Sanitized downstream in
        # _team_from_router_roster, never trusted as-is.
        self.agents = agents


class _HostedFunctionCall:
    """Content shim mimicking an agent_framework function_call (.name/.arguments)."""

    type = "function_call"

    def __init__(self, name: Optional[str], arguments: str = "") -> None:
        self.name = name
        self.arguments = arguments


class _HostedFunctionResult:
    """Content shim mimicking an agent_framework function_result (.name/.exception)."""

    type = "function_result"

    def __init__(
        self,
        name: Optional[str],
        result: Optional[object] = None,
        exception: Optional[object] = None,
    ) -> None:
        # name: tool name
        # result: optional result payload
        # exception: optional exception / error payload (truthy indicates failure)
        self.name = name
        self.result = result
        self.exception = exception


class _HostedCodeInterpreterToolCall:
    """Hosted shim for code interpreter call activity."""

    type = "code_interpreter_tool_call"

    def __init__(self, input: str = "", arguments: str = "") -> None:
        self.input = input
        self.arguments = arguments


class _HostedCodeInterpreterToolResult:
    """Hosted shim for code interpreter result activity."""

    type = "code_interpreter_tool_result"

    def __init__(
        self,
        output: str = "",
        stdout: str = "",
        stderr: str = "",
        annotations: Optional[list] = None,
    ) -> None:
        self.output = output
        self.stdout = stdout
        self.stderr = stderr
        self.annotations = annotations or []


class _HostedHostedFile:
    """Hosted shim for generated/container file metadata."""

    type = "hosted_file"

    def __init__(
        self,
        file_id: Optional[str] = None,
        name: Optional[str] = None,
        additional_properties: Optional[dict] = None,
    ) -> None:
        self.file_id = file_id
        self.name = name
        self.additional_properties = additional_properties or {}


class _HostedMcpServerToolCall:
    """Hosted shim for a Toolbox/MCP tool call (attribute shape matches the SSE
    handler's ``mcp_server_tool_call`` branch: tool_name/server_name/arguments)."""

    type = "mcp_server_tool_call"

    def __init__(
        self,
        tool_name: Optional[str] = None,
        server_name: Optional[str] = None,
        arguments: Optional[str] = None,
    ) -> None:
        self.tool_name = tool_name
        self.server_name = server_name
        self.arguments = arguments


class _HostedMcpServerToolResult:
    """Hosted shim for a Toolbox/MCP tool result (matches the SSE handler's
    ``mcp_server_tool_result`` branch: tool_name/server_name/status)."""

    type = "mcp_server_tool_result"

    def __init__(
        self,
        tool_name: Optional[str] = None,
        server_name: Optional[str] = None,
        status: Optional[str] = None,
        output: Optional[str] = None,
    ) -> None:
        self.tool_name = tool_name
        self.server_name = server_name
        self.status = status
        self.output = output


class _HostedUpdate:
    """Update shim mimicking an agent_framework streaming update (.contents)."""

    def __init__(self, contents: list) -> None:
        self.contents = contents


_HOSTED_ORCHESTRATOR_INSTRUCTIONS = (
    "You are a helpful assistant with a Toolbox of tools — including "
    "knowledge-base retrieval (knowledge_base_retrieve) over the user's "
    "authoritative sources — and a code interpreter. Use knowledge_base_retrieve "
    "when the user asks about their documents, organization, or domain knowledge. "
    "Use the other Toolbox tools when the task needs external data or actions, and "
    "the code interpreter to run code, do computation/analysis, and produce "
    "downloadable files when the user asks for an artifact. You do NOT have a "
    "persistent write-memory tool: within a conversation you remember from context, "
    "but never claim to have permanently saved something you did not. Keep your "
    "answers brief."
)

# api-version that enables the model's direct Responses API + code-interpreter
# containers. Used by both the chat invoke and the file-download endpoint so the
# container is created and read through the SAME scope (a mismatched scope 404s
# with "Container not found").
_DIRECT_RESPONSES_API_VERSION = "2025-03-01-preview"


# Router function catalog: the coarse capability the Model Router
# (chat/completions) can signal via a `function` tool call. chat/completions
# accepts `function` tools but NOT hosted `code_interpreter`/`mcp` (verified
# live), so this is HOW the router hands a code/file task down to the Responses
# execution layer. Everything else (Toolbox, agents via FoundryMCPServer's
# agent_invoke) is attached dynamically IN that layer, not here.
def _capability(
    name: str,
    description: str,
    arg: str,
    arg_desc: str,
    extra_properties: Optional[dict] = None,
) -> dict:
    properties: dict = {arg: {"type": "string", "description": arg_desc}}
    if extra_properties:
        properties.update(extra_properties)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [arg],
            },
        },
    }


# The Model Router only ever sees CAPABILITIES (user intentions), never
# implementations. The backend owns the capability -> implementation mapping (see
# the dispatch in _RouterChatClient.invoke): e.g. run_python_execution ->
# Responses+code interpreter, run_macae_mcp_server -> ca-mcp DIRECT (+identity),
# run_knowledge_base -> Toolbox KBs, run_web_search -> native web_search. New
# capabilities are one entry here + one dispatch branch — the router stays a
# small semantic classifier and never learns Responses/MCP/Toolbox exist.
_ROUTER_FUNCTIONS = [
    _capability(
        "run_python_execution",
        "The request needs running Python code, computation, data analysis, or "
        "producing a downloadable file (script, CSV, chart, plot, etc.).",
        "task",
        "The full, self-contained task to execute.",
    ),
    _capability(
        "run_image_generation",
        "The request asks to GENERATE or EDIT an IMAGE — a picture, product "
        "shot, campaign visual, illustration, logo, or photo-realistic scene. "
        "NOT questions ABOUT images and NOT data charts/plots (that is "
        "run_python_execution).",
        "task",
        "A complete, self-contained image prompt: subject, style, "
        "composition, lighting, aspect/mood.",
    ),
    _capability(
        "run_macae_mcp_server",
        "The request needs to USE the tools of an external connected server — "
        "e.g. Infobip, GitHub (list branches, PRs, issues, files, commits), ARM (list "
        "resources, subscriptions), Grafana (dashboards, metrics), Outlook/mail "
        "(send/read email), or any other server available in the Toolbox. "
        "The task MUST be worded as a direct tool action (e.g. 'list branches of "
        "repo X using GitHub tools', 'send email via Outlook tools') so the "
        "execution model calls the tool directly. "
        "NOT the user's knowledge bases (that is run_knowledge_base) and NOT "
        "public web search (that is run_web_search). "
        "Reading or summarizing the CONTENT of a file from GitHub or any "
        "external repo/system is ALWAYS this capability, never "
        "run_knowledge_base. "
        "Never answer from memory; this fetches real data via the tools.",
        "task",
        "The full, self-contained task expressed as a DIRECT tool action "
        "(e.g. 'Use GitHub___list_branches to list branches of owner=X repo=Y').",
    ),
    _capability(
        "run_knowledge_base",
        "The request asks about the user's OWN documents, organization, domain "
        "knowledge, or indexed sources — answerable from the user's knowledge bases "
        "(Foundry IQ / Azure AI Search). NOT public web search, NOT a connected "
        "external server, and NOT reading files/code from GitHub or external "
        "repos (that is run_macae_mcp_server). Retrieves authoritative, "
        "source-attributable content.",
        "task",
        "A well-formed query describing the information to retrieve from the KB.",
    ),
    _capability(
        "run_foundry_mcp",
        "The request is about managing/operating Azure AI Foundry ITSELF — Foundry "
        "agents (list/get/create/update/invoke/delete), models (catalog, deploy, "
        "benchmark, quotas, monitoring), evaluations and evaluators, datasets, "
        "prompt optimization, project connections, or Foundry sessions. Uses the "
        "native Foundry MCP Server. NOT the user's own connected external servers "
        "(that is run_macae_mcp_server) and NOT knowledge bases.",
        "task",
        "The full, self-contained task for the Foundry MCP Server.",
    ),
    _capability(
        "run_web_search",
        "The request needs CURRENT PUBLIC information from the live internet — "
        "news, recent events, latest releases/announcements, docs, prices, or "
        "anything likely newer than your training data. Runs a real web search.",
        "task",
        "A well-formed web search query for the information needed.",
    ),
    _capability(
        "run_plan",
        "The request needs the formal multi-agent PLAN: a Magentic orchestration "
        "where SEVERAL SPECIALIZED agents collaborate on one composite objective "
        "under a manager, with human approval/clarification steps and progress "
        "tracked on a dedicated Plan page (not an inline chat answer). Pick this "
        "when the work spans clearly SEPARATE domains of responsibility that must "
        "be coordinated across distinct agents to be done correctly, OR when the "
        "user explicitly asks to create/prepare/run a plan or to orchestrate a "
        "multi-step effort. A single agent with tools (the other capabilities) "
        "finishes ordinary tasks end-to-end — do NOT pick this for anything one "
        "agent can complete alone.",
        "task",
        "The full, self-contained objective for the multi-agent plan, as the user "
        "expressed it.",
        extra_properties={
            # The Router composes the roster in the SAME call that escalates —
            # no second model round-trip. The backend sanitizes and materializes
            # it (factory constraints re-checked in code) before the Magentic
            # graph is built; the roster is frozen at build().
            "agents": {
                "type": "array",
                "description": (
                    "The minimal team of specialist agents for this plan — 1 to "
                    "4 entries, fewer is better; derive them from the request "
                    "itself. Specialists are REUSED by name across plans, so "
                    "write system_message as reusable role instructions (what "
                    "the specialist is and does), never one-task orders. Do not "
                    "include a proxy/manager/orchestrator entry. Omit this field "
                    "only if the user explicitly asks to use their currently "
                    "selected team."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "PascalCase, ending in 'Agent', unique in the "
                                "roster (e.g. DataAgent)."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": "One line: what this specialist is for.",
                        },
                        "system_message": {
                            "type": "string",
                            "description": (
                                "Reusable role instructions for the specialist."
                            ),
                        },
                        "coding_tools": {
                            "type": "boolean",
                            "description": (
                                "true ONLY if it must run code or produce real "
                                "downloadable files (scripts, spreadsheets, "
                                "packages, charts)."
                            ),
                        },
                        "use_mcp": {
                            "type": "boolean",
                            "description": (
                                "true ONLY if it needs external systems or live data."
                            ),
                        },
                        "use_bing": {
                            "type": "boolean",
                            "description": (
                                "true ONLY if it needs LIVE public web information "
                                "(current prices, news, market data). Never "
                                "together with use_reasoning."
                            ),
                        },
                        "use_reasoning": {
                            "type": "boolean",
                            "description": (
                                "Deep multi-step analysis. Never together with "
                                "coding_tools."
                            ),
                        },
                    },
                    "required": ["name", "description", "system_message"],
                },
            }
        },
    ),
    # NOTE: there is NO "respond directly" capability. Answering the user is not a
    # capability — it is the DEFAULT. When the router picks none of the above, the
    # dispatch `else` branch routes the prompt through the same _execute_responses
    # path (no tools), so every turn — tool or not — chains through one memory path.
]


class _RouterChatClient:
    """Chat client for the direct-response path. **The entry point is the Model
    Router**, NOT the Foundry Hosted Agent (the class name ``agent_name`` arg is
    only a display label — no hosted agent is ever called; verified live by the
    hit URLs).

    Flow (``invoke``):
    1. Petition -> **Model Router** (``model-router`` deployment,
       ``{account}/openai/deployments/model-router/chat/completions``) with a
       single ``function`` tool ``run_code_interpreter``. The router picks the
       best/cheapest model AND signals — via that ``function_call`` — whether the
       task needs code execution.
    2. No ``function_call`` -> stream the router's own text answer straight
       through (already the routed, appropriate model).
    3. ``run_code_interpreter`` ``function_call`` -> ``_invoke_execution`` runs
       o4-mini on the **direct Responses API** with a native code interpreter +
       the Toolbox as an MCP tool, and surfaces a downloadable file.

    Why the execution layer talks to the model DIRECTLY (not the Hosted Agent):
    the hosted runtime re-serves its inner agent over
    ``.../protocols/openai/responses`` but **silently drops the
    ``code_interpreter_call`` items and the container reference** — verified
    live: over that endpoint only ``reasoning`` + ``message`` items ever reach
    the client (stream, non-stream, and ``store=True`` retrieve all agree),
    ``container_id``/``file_id`` never appear, so a generated file surfaces only
    as a dead ``sandbox:/mnt/data/...`` link and can never be downloaded.

    Calling the model directly with the **Toolbox attached as an MCP tool** +
    a native **code interpreter** reproduces exactly what the Hosted Agent did
    (tool search over the same server-side Toolbox — verified: ``mcp_list_tools``
    returns the Toolbox tools) AND exposes ``container_id`` on the
    ``code_interpreter_call`` plus a ``container_file_citation`` annotation
    (``file_id`` + ``container_id`` + ``filename``) on the assistant message —
    which the ``/chat/download-file/{file_id}?container_id=...`` endpoint turns
    into a real download.

    This adapter keeps ``FoundryAgentTemplate.invoke()``'s contract — yields
    updates exposing ``.contents`` — so the SSE streaming handler is reused
    unchanged. It never publishes or mutates any agent.
    """

    def __init__(
        self,
        agent_name: str,
        user_access_token: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> None:
        from common.config.app_config import config

        self.agent_name = agent_name
        self._last_response_id: Optional[str] = None
        self._workspace_id = workspace_id
        # End-user access token (EasyAuth/Bearer) for on-behalf-of calls. When
        # present, the call is made as the *user*, not the app Managed Identity
        # — required so Foundry propagates a real delegated user context to the
        # Toolbox's user-scoped connectors.
        self._user_access_token = user_access_token
        # Authenticated user id (principal). Sent to ca-mcp DIRECTLY as the
        # x-ms-client-principal-id header (see run_macae_mcp_server) so ca-mcp
        # resolves the real user for its Cosmos connection lookups — deterministic,
        # from the backend, outside the prompt (validated: sentinel round-trips on a
        # DIRECT attach; the Foundry Toolbox proxy strips it).
        self._user_id = user_id or ""
        self._user_cred = None
        # AZURE_AI_PROJECT_ENDPOINT is {account}/api/projects/{project}. The
        # model's OpenAI-compatible Responses API lives at the ACCOUNT root
        # ({account}/openai, api-version 2025-03-01-preview — the first version
        # that enables the Responses API), while the Toolbox MCP lives under the
        # project ({project}/toolboxes/{name}/mcp).
        project = (config.AZURE_AI_PROJECT_ENDPOINT or "").rstrip("/")
        account = project.split("/api/projects/")[0]
        self._openai_base_url = f"{account}/openai"
        self._api_version = _DIRECT_RESPONSES_API_VERSION
        self._model = config.CHAT_ORCHESTRATOR_MODEL
        # Image generation (gpt-image family). Deployment must exist in the
        # Foundry account; override via env when the name differs.
        self._image_deployment = config._get_optional(
            "IMAGE_GENERATION_DEPLOYMENT", "gpt-image-2"
        )
        self._image_api_version = config._get_optional(
            "IMAGE_GENERATION_API_VERSION", "2025-04-01-preview"
        )
        # ONE definition of how this project reaches a Foundry toolbox: name,
        # pinned version, URL shape and the preview gate. Both the attach below
        # and anything else that needs a toolbox read it from here — the same
        # contract written twice is what made the bridge answer 401 while this
        # path worked.
        self._toolboxes: list[tuple[str, str]] = []
        for _spec in (config.CHAT_TOOLBOXES or "").split(","):
            _spec = _spec.strip()
            if not _spec:
                continue
            _name, _, _version = _spec.partition(":")
            _name, _version = _name.strip(), _version.strip()
            if not _name:
                continue
            _segment = f"/versions/{_version}" if _version else ""
            self._toolboxes.append(
                (_name, f"{project}/toolboxes/{_name}{_segment}/mcp?api-version=v1")
            )
        # ca-mcp (MacaeMcpServer) DIRECT endpoint — attached without the Foundry
        # Toolbox proxy so the x-ms-client-principal-id identity header reaches
        # ca-mcp. Must be reachable by the Azure model service (public); see
        # MACAE_MCP_PUBLIC_ENDPOINT (dev localhost is unreachable → point it at the
        # deployed ca-mcp).
        self._macae_mcp_url = config.MACAE_MCP_PUBLIC_ENDPOINT or ""
        # Foundry MCP Server (preview) — native Foundry MCP, attached DIRECTLY as
        # the run_foundry_mcp capability. Entra-OAuth protected: its token needs the
        # FOUNDRY_MCP_SCOPE (Foundry.Mcp.Tools), minted separately from the
        # ai.azure.com token in _bearer (see _foundry_mcp_bearer).
        self._foundry_mcp_url = (
            config.FOUNDRY_MCP_ENDPOINT or "https://mcp.ai.azure.com"
        )
        self._foundry_mcp_scope = (
            config.FOUNDRY_MCP_SCOPE or "https://mcp.ai.azure.com/.default"
        )
        # Model Router front-door (chat/completions). It routes to the best model
        # AND, via the run_code_interpreter function tool, signals when a task
        # needs the Responses execution layer. It CANNOT carry code_interpreter/mcp
        # and is not usable on Responses or as an agent model (all verified), so it
        # stays a pure front-door decision maker.
        self._router_model = config.CHAT_ROUTER_MODEL
        self._router_base_url = (
            f"{account}/openai/deployments/{config.CHAT_ROUTER_MODEL}"
        )
        self._router_api_version = config.CHAT_ROUTER_API_VERSION

    async def _bearer(self) -> str:
        from common.config.app_config import config

        # Prefer the end user's identity (OBO) ONLY when OBO is provisioned.
        # The hosted agent's Toolbox lists user-scoped connectors (WorkIQ
        # Teams/Mail, outlook, arm, Foundry, AzureDevOps) that REJECT an
        # application (Managed Identity) caller — "requires a delegated Microsoft
        # Entra user context" — and a single failed source aborts the whole
        # tools/list, so the agent emits nothing. Calling on-behalf-of the user
        # makes Foundry propagate a real user context to the Toolbox.
        #
        # The ENABLE_OBO gate is deliberate: without it, build_user_credential
        # returns a passthrough of the raw user token (wrong audience for the
        # Foundry data plane) which would break the call. In local dev
        # (ENABLE_OBO off) we fall through to the shared credential, which
        # resolves to the developer's az-login user anyway — so dev keeps working.
        if self._user_access_token and config.ENABLE_OBO:
            if self._user_cred is None:
                self._user_cred = config.build_user_credential(self._user_access_token)
            _cred_bearer = self._user_cred
            if _cred_bearer is not None:
                token = await _cred_bearer.get_token("https://ai.azure.com/.default")
                return token.token

        # No user token / OBO not enabled: borrow the process-shared credential
        # (app identity in prod, az-login user in dev). Never closed here — owned
        # by the app lifespan.
        cred = config.get_shared_async_credential()
        token = await cred.get_token("https://ai.azure.com/.default")
        return token.token

    async def _foundry_mcp_bearer(self) -> str:
        """Token for the Foundry MCP Server (preview). Its OAuth metadata requires
        the scope ``https://mcp.ai.azure.com/Foundry.Mcp.Tools`` — a DIFFERENT
        audience than ai.azure.com — so it is minted separately from ``_bearer``.
        On-behalf-of the user when OBO is provisioned (the server runs OBO with the
        user's Entra identity and needs Contributor on the project), else the
        process-shared credential (app MI in prod, az-login user in dev).
        """
        from common.config.app_config import config

        if self._user_access_token and config.ENABLE_OBO:
            if self._user_cred is None:
                self._user_cred = config.build_user_credential(self._user_access_token)
            _cred_mcp = self._user_cred
            if _cred_mcp is not None:
                token = await _cred_mcp.get_token(self._foundry_mcp_scope)
                return token.token
        cred = config.get_shared_async_credential()
        token = await cred.get_token(self._foundry_mcp_scope)
        return token.token

    async def invoke(
        self,
        prompt: str,
        history: Optional[list] = None,
        allow_plan: bool = True,
        file_ids: Optional[list[str]] = None,
        **_ignored,
    ):
        """Entry point: the Model Router picks a CAPABILITY, the backend runs it.

        The petition hits the deployed **Model Router** (chat/completions), which
        routes to the best model AND — via the ``_ROUTER_FUNCTIONS`` capability
        catalog — signals the user's INTENT as a ``function_call``. Memory comes
        from ``history`` (rebuilt from Cosmos + Azure AI Search, NOT
        previous_response_id) passed into the router's ``messages`` and the
        execution ``input``. This method maps each capability to its implementation:

        * ``run_python_execution`` -> ``_execute_responses`` with code interpreter
          (o4-mini via Responses -> downloadable file).
        * ``run_macae_mcp_server`` -> ``_execute_responses`` with ca-mcp attached
          DIRECTLY + the identity header (connected external MCP servers, with the
          real user resolved; forces a real tool call, no fabricating).
        * ``run_knowledge_base`` -> ``_execute_responses`` with the Toolbox MCP
          (the user's KBs / Foundry IQ knowledge_base_retrieve).
        * ``run_web_search`` -> ``_execute_responses`` with the native web_search.
        * no capability picked -> the router already answered directly, streamed
          live from its OWN selected model (with ``history`` for memory). Answering
          is not a capability, so nothing more runs.

        Tool turns target o4-mini (a tool-capable deployment); no-tool turns are
        answered by whichever model the router selected.
        """
        from openai import AsyncOpenAI

        bearer = await self._bearer()
        router = AsyncOpenAI(
            api_key=bearer,
            base_url=self._router_base_url,
            default_query={"api-version": self._router_api_version},
            timeout=120,
        )
        from v4.api.router_decision import RouterDecisionAccumulator

        _acc = RouterDecisionAccumulator()
        _router_answered = False
        # history (Cosmos + AI Search) gives the router memory; chat/completions is
        # stateless, so the conversation is supplied as prior messages.
        # Anchoring rule: recovered history mixes cross-session retrieval with
        # this session's turns (short memory comes LAST, adjacent to the user
        # message). Without the rule, a bare "si, adelante" binds to whatever
        # old retrieved turns dominate and the router fabricates tool tasks
        # from them (reproduced live: post-plan affirmation -> invented
        # code-interpreter job).
        _messages = (
            [
                {
                    "role": "system",
                    "content": (
                        "Ground every decision in the CURRENT user message and the "
                        "immediately preceding assistant message of this "
                        "conversation. When the current message is a bare "
                        "acknowledgement or continuation (e.g. 'si', 'ok', "
                        "'adelante', 'correcto', 'continua'), it refers ONLY to "
                        "that immediately preceding assistant message — answer in "
                        "that context. NEVER pick a capability based on older or "
                        "retrieved context alone: a capability requires an "
                        "explicit request in the CURRENT message. If the "
                        "preceding assistant message presented a completed "
                        "multi-agent plan, continue that discussion directly "
                        "instead of launching tools."
                    ),
                }
            ]
            + list(history or [])
            + [{"role": "user", "content": prompt}]
        )
        # UI chat|plan selector in Chat position: the message may never become a
        # plan, so run_plan is not even offered to the router.
        _tools = (
            _ROUTER_FUNCTIONS
            if allow_plan
            else [t for t in _ROUTER_FUNCTIONS if t["function"]["name"] != "run_plan"]
        )
        try:
            stream = await router.chat.completions.create(
                model=self._router_model,
                messages=cast(Any, _messages),
                tools=cast(Any, _tools),
                stream=True,
            )
            async for chunk in stream:
                choice = chunk.choices[0] if getattr(chunk, "choices", None) else None
                if choice is None:
                    continue
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                for tc in getattr(delta, "tool_calls", None) or []:
                    _acc.add_delta(tc)
                # Stream the router's OWN answer live for no-tool turns: it comes
                # from the model the router selected AND remembers (history is in
                # messages). Tool turns emit tool_calls with no content, so this
                # only fires for direct replies. Guarded on has_function so a late
                # tool_call never mixes with streamed text.
                content = getattr(delta, "content", None)
                if content and not _acc.has_function:
                    _router_answered = True
                    yield _HostedUpdate([_HostedTextContent(content)])
        finally:
            await router.close()

        _decision = _acc.finalize()
        _fn_name = _decision.fn_name
        _args = _decision.args
        if _decision.parse_error is not None:
            # This used to be a bare `except: _args = {}` and the dispatch then
            # ran on the RAW user prompt with no trace — invisible misrouting.
            logger.warning(
                "Router args UNPARSEABLE for %s — dispatch will fall back to the "
                "raw user prompt. raw=%r",
                _fn_name,
                _decision.parse_error[:300],
            )

        # Capability -> implementation dispatch. The router chose a CAPABILITY
        # (intention); the backend maps it to the execution that serves it. Add a
        # capability = one entry in _ROUTER_FUNCTIONS + one branch here; the router
        # never learns the implementation.
        logger.info(
            "Router decision: function=%s args=%s",
            _fn_name or "<none>",
            _args,
        )
        if _fn_name and "task" not in _args:
            # Valid-JSON-but-empty args: every dispatch branch below does
            # `_args.get("task") or prompt`, so this turn will run on the RAW
            # user prompt. Say it loudly instead of letting it look routed.
            logger.warning(
                "Router picked %s but returned NO task arg — dispatching the "
                "RAW user prompt.",
                _fn_name,
            )
        if _fn_name == "run_python_execution":
            logger.info("Dispatching run_python_execution")
            task = _args.get("task") or prompt
            async for update in self._redact_via_router(
                self._execute_responses(task, history, use_code_interpreter=True),
                _fn_name,
                _args,
                _messages,
            ):
                yield update
        elif _fn_name == "run_image_generation":
            logger.info("Dispatching run_image_generation")
            task = _args.get("task") or prompt
            async for update in self._execute_image_generation(task):
                yield update
        elif _fn_name == "run_macae_mcp_server":
            logger.info("Dispatching run_macae_mcp_server")
            task = _args.get("task") or prompt
            async for update in self._redact_via_router(
                self._execute_responses(task, history, use_macae=True),
                _fn_name,
                _args,
                _messages,
            ):
                yield update
        elif _fn_name == "run_knowledge_base":
            logger.info("Dispatching run_knowledge_base")
            task = _args.get("task") or prompt
            async for update in self._redact_via_router(
                self._execute_responses(task, history, use_toolbox=True),
                _fn_name,
                _args,
                _messages,
            ):
                yield update
        elif _fn_name == "run_foundry_mcp":
            logger.info("Dispatching run_foundry_mcp")
            task = _args.get("task") or prompt
            async for update in self._redact_via_router(
                self._execute_responses(task, history, use_foundry=True),
                _fn_name,
                _args,
                _messages,
            ):
                yield update
        elif _fn_name == "run_web_search":
            logger.info("Dispatching run_web_search")
            task = _args.get("task") or prompt
            async for update in self._redact_via_router(
                self._execute_responses(task, history, use_web_search=True),
                _fn_name,
                _args,
                _messages,
            ):
                yield update
        elif _fn_name == "run_plan" and not allow_plan:
            # Belt over the tools filter above: in Chat position a plan signal
            # must never surface — answer the task inline instead.
            logger.info("run_plan suppressed (allow_plan=False) — answering inline")
            task = _args.get("task") or prompt
            async for update in self._execute_responses(task, history):
                yield update
        elif _fn_name == "run_plan":
            logger.info("Dispatching run_plan (escalate to Magentic orchestration)")
            task = _args.get("task") or prompt
            # Signal only: the Plan runs via process_request's orchestration path
            # (BackgroundTask + WebSocket + PlanPage), NOT inline on this SSE turn.
            # The SSE handler turns this signal into plan creation + plan_created.
            # The roster the Router proposed rides along; the backend sanitizes
            # and materializes it before the graph is built.
            _roster = _args.get("agents")
            yield _HostedUpdate(
                [
                    _HostedPlanSignal(
                        task, agents=_roster if isinstance(_roster, list) else None
                    )
                ]
            )
        elif _router_answered:
            # No capability AND the router already streamed its own answer above
            # (its selected model, with history for memory). Nothing more to run.
            logger.info("Direct answer streamed by the router's selected model")
        else:
            # Router emitted neither a tool nor any text: fall back to o4-mini +
            # Toolbox so the turn still gets memory/knowledge and an answer.
            logger.info("Router produced nothing; falling back to o4-mini execution")
            async for update in self._execute_responses(prompt, history):
                yield update

    async def _redact_via_router(self, gen, fn_name: str, args: dict, messages: list):
        """Responses ejecuta; el Router redacta.

        La actividad de herramienta y los artefactos (container_id/file_id/
        anotaciones) salen tal cual — el Router no puede transportarlos. El
        TEXTO vuelve como resultado ``tool`` y el Router escribe la respuesta
        final con el modelo que eligió, igual que en los turnos sin herramienta.
        """
        from openai import AsyncOpenAI

        text: list[str] = []
        async for update in gen:
            keep = [c for c in update.contents if getattr(c, "type", None) != "text"]
            text += [
                getattr(c, "text", "") or ""
                for c in update.contents
                if getattr(c, "type", None) == "text"
            ]
            if keep:
                yield _HostedUpdate(keep)

        call_id = f"call_{fn_name}"
        _msgs = list(messages) + [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": fn_name,
                            "arguments": json.dumps(args or {}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": "".join(text).strip() or f"({fn_name}: no text output)",
            },
        ]
        router = AsyncOpenAI(
            api_key=await self._bearer(),
            base_url=self._router_base_url,
            default_query={"api-version": self._router_api_version},
            timeout=120,
        )
        try:
            # Sin `tools`: esta pasada sólo redacta, no encadena otra herramienta.
            stream = await router.chat.completions.create(
                model=self._router_model, messages=cast(Any, _msgs), stream=True
            )
            async for chunk in stream:
                choice = chunk.choices[0] if getattr(chunk, "choices", None) else None
                delta = getattr(choice, "delta", None) if choice else None
                content = getattr(delta, "content", None) if delta else None
                if content:
                    yield _HostedUpdate([_HostedTextContent(content)])
        finally:
            await router.close()

    def _spawn_container_file_persist(
        self,
        file_id: Optional[str],
        container_id: Optional[str],
        filename: Optional[str],
    ) -> None:
        """Copy a code-interpreter output file to the persistent store the
        moment it exists — its Foundry container expires minutes later.

        Fetches with THIS client's bearer (OBO or shared): a user-scoped
        container is invisible to any other identity (same rule as
        /chat/download-file).
        """
        if not file_id or not container_id:
            return

        async def _run() -> None:
            from openai import AsyncOpenAI

            from v4.common.services.generated_file_store import GeneratedFileStore

            bearer = await self._bearer()
            client = AsyncOpenAI(
                api_key=bearer,
                base_url=self._openai_base_url,
                default_query={"api-version": _DIRECT_RESPONSES_API_VERSION},
                timeout=120,
            )
            try:
                name = filename
                if not name:
                    info: Any = await client.containers.files.retrieve(
                        file_id=file_id, container_id=container_id
                    )
                    path = getattr(info, "path", None) or file_id
                    name = os.path.basename(path)
                content = await client.containers.files.content.retrieve(
                    file_id=file_id, container_id=container_id
                )
                data = await content.aread()
            finally:
                await client.close()
            await GeneratedFileStore.get_instance().save(file_id, name or file_id, data)
            if self._workspace_id:
                from v4.common.services.workspace_service import (
                    _git,
                    _resolve,
                    workspace_for,
                )

                try:
                    _ws = workspace_for(self._user_id, self._workspace_id)
                    _dest = _resolve(_ws, name or file_id)
                    _dest.parent.mkdir(parents=True, exist_ok=True)
                    _dest.write_bytes(data)
                    _rel = str(_dest.relative_to(_ws))
                    _git(_ws, "add", _rel)
                    _git(_ws, "commit", "-q", "-m", f"agent: add {name or file_id}")
                except Exception as _ws_err:
                    logger.warning(
                        "workspace write failed for file_id=%s: %s", file_id, _ws_err
                    )

        _spawn_bg_persist(_run(), f"codeinterp:{file_id}")

    async def _execute_image_generation(self, prompt: str):
        """Generate an image with the configured gpt-image deployment and
        surface it through the EXISTING generated_file channel: the bytes are
        uploaded to Foundry files, so /chat/download-file and the frontend
        renderers (chat AND plan surfaces) work unchanged.
        """
        import base64

        import httpx

        bearer = await self._bearer()
        # Route verified live with an empty-body probe (2026-07-28): the
        # ACCOUNT-level deployment path answers (400 "Missing 'prompt'") on
        # api-version 2025-04-01-preview; "2026-04-21" is the MODEL version
        # from the catalog card, not an API version (404s). The project-scoped
        # path is a different auth plane (401, audience ai.azure.com).
        url = (
            f"{self._openai_base_url}/deployments/{self._image_deployment}"
            f"/images/generations?api-version={self._image_api_version}"
        )
        logger.info(
            "Image generation: model=%s prompt=%s",
            self._image_deployment,
            prompt[:200],
        )
        try:
            async with httpx.AsyncClient(timeout=300.0) as http:
                resp = await http.post(
                    url,
                    headers={"Authorization": f"Bearer {bearer}"},
                    json={
                        "prompt": prompt,
                        "n": 1,
                        # 3:2 nativo — el mismo ratio de la tarjeta del chat:
                        # la imagen llena la caja sin recorte ni bandas.
                        # Validado en el playground sobre este deployment.
                        "size": "1536x1024",
                    },
                )
        except Exception as ex:
            logger.error("Image generation request failed: %s", ex)
            yield _HostedUpdate([_HostedTextContent(f"Image generation failed: {ex}")])
            return
        if resp.status_code != 200:
            # Surface the FULL error — an empty/summarized failure here is how
            # models end up narrating images they never generated.
            err = (
                f"Image generation failed: HTTP {resp.status_code} — {resp.text[:300]}"
            )
            logger.error(err)
            yield _HostedUpdate([_HostedTextContent(err)])
            return
        payload = resp.json()
        b64 = ((payload.get("data") or [{}])[0] or {}).get("b64_json")
        if not b64:
            yield _HostedUpdate(
                [
                    _HostedTextContent(
                        "Image generation returned no image data: " + str(payload)[:200]
                    )
                ]
            )
            return
        image_bytes = base64.b64decode(b64)
        filename = f"generated_{uuid.uuid4().hex[:8]}.png"

        from azure.ai.agents.aio import AgentsClient

        from common.config.app_config import config

        # Borrowed shared credential — per-request minting leaked the
        # credential's aiohttp ClientSession (nothing closed it).
        creds = config.get_shared_async_credential()
        async with AgentsClient(
            endpoint=config.AZURE_AI_PROJECT_ENDPOINT,
            credential=creds,
        ) as agents_client:
            uploaded = await agents_client.files.upload(
                file=(filename, image_bytes, "image/png"),
                purpose="assistants",
            )
        logger.info(
            "Generated image uploaded to Foundry: file_id=%s name=%s size=%d",
            uploaded.id,
            filename,
            len(image_bytes),
        )
        # Persist at generation time — the bytes are already in hand.
        from v4.common.services.generated_file_store import GeneratedFileStore

        _spawn_bg_persist(
            GeneratedFileStore.get_instance().save(uploaded.id, filename, image_bytes),
            f"imagegen:{uploaded.id}",
        )
        # hosted_file → the SSE handler emits the generated_file event with the
        # download_url and persists it in the assistant message metadata — the
        # same end-to-end channel code_interpreter files already use.
        yield _HostedUpdate([_HostedTextContent("Imagen generada:")])
        yield _HostedUpdate(
            [
                _HostedHostedFile(
                    file_id=uploaded.id,
                    name=filename,
                    additional_properties={"filename": filename},
                )
            ]
        )

    def _toolbox_tools(self, bearer: str) -> list[dict[str, Any]]:
        """MCP attach entries for every declared Foundry toolbox.

        `Foundry-Features` is not optional: the toolbox endpoint is preview-
        gated and answers 401 without it, however valid the token is.
        """
        return [
            {
                "type": "mcp",
                "server_label": label,
                "server_url": url,
                "require_approval": "never",
                "headers": {
                    "Authorization": f"Bearer {bearer}",
                    "Foundry-Features": "Toolboxes=V1Preview",
                },
            }
            for label, url in self._toolboxes
        ]

    async def _execute_responses(
        self,
        prompt: str,
        history: Optional[list] = None,
        file_ids: Optional[list[str]] = None,
        *,
        use_code_interpreter: bool = False,
        use_toolbox: bool = False,
        use_web_search: bool = False,
        use_macae: bool = False,
        use_foundry: bool = False,
    ):
        """Execution layer: o4-mini via the direct Responses API. Tools per
        capability:
        * ``use_macae`` (run_macae_mcp_server) -> ca-mcp (MacaeMcpServer) attached
          DIRECTLY (its own endpoint, NOT the Foundry Toolbox) with the
          x-ms-client-principal-id identity header. The Toolbox proxy strips that
          header, so the direct attach is what makes ca-mcp resolve the real user.
          The Toolbox is NOT attached here; its KBs are a separate capability.
        * otherwise the Toolbox MCP is attached (memory + KBs + external tools); the
          code interpreter is added for ``run_python_execution`` and the NATIVE
          Responses ``web_search`` tool for ``run_web_search``.
        Yields the same content shims, so the SSE handler and download-file flow are
        untouched.
        """
        from openai import AsyncOpenAI

        from common.config.app_config import config

        # Attach map: foundry-MCP turns attach Foundry's MCP; macae turns attach
        # ca-mcp DIRECTLY via its public endpoint (Toolbox only as fallback when
        # no public endpoint is configured); every other turn attaches the Toolbox.
        _macae_direct = bool(use_macae and self._macae_mcp_url)
        logger.info(
            "Responses tools: code_interpreter=%s web_search=%s foundry_direct=%s "
            "macae_direct=%s (url=%s) toolbox=%s",
            use_code_interpreter,
            use_web_search,
            use_foundry,
            _macae_direct,
            (self._macae_mcp_url or "<none>") if use_macae else "-",
            not use_foundry and not _macae_direct,
        )

        # Official OpenAI SDK pointed at the model's direct Responses API
        # (account /openai endpoint). base_url + default_query yield
        # {account}/openai/responses?api-version=2025-03-01-preview.
        bearer = await self._bearer()
        client = AsyncOpenAI(
            api_key=bearer,
            base_url=self._openai_base_url,
            default_query={"api-version": self._api_version},
            timeout=180,
        )
        # Base tool set. For run_macae_mcp_server we attach ca-mcp (MacaeMcpServer)
        # DIRECTLY — NOT via the Foundry Toolbox — because the Toolbox proxy strips
        # the x-ms-client-principal-id header (proven live) and ca-mcp would fall
        # back to a placeholder user, never matching how the user's connection was
        # stored. A DIRECT attach carries the identity header to ca-mcp, which reads
        # it (verified: sentinel round-trips), so its Cosmos connection lookups run
        # as the real user — deterministic, from the backend, outside the prompt.
        # Every OTHER execution turn attaches the Foundry Toolbox (memory + KBs +
        # external tools). require_approval="never" runs tool calls without pausing.
        if use_foundry:
            # Foundry MCP Server (preview), native, attached DIRECTLY. Entra-OAuth
            # protected → needs a token for the Foundry.Mcp.Tools scope (a DIFFERENT
            # audience than ai.azure.com), minted by _foundry_mcp_bearer. Verified
            # live: this attach exposes the full ~75-tool server with no 424.
            foundry_bearer = await self._foundry_mcp_bearer()
            tools: list = [
                {
                    "type": "mcp",
                    "server_label": "FoundryMCPServer",
                    "server_url": self._foundry_mcp_url,
                    "require_approval": "never",
                    "headers": {"Authorization": f"Bearer {foundry_bearer}"},
                }
            ]
        elif use_macae:
            # DIRECT attach of ca-mcp via its PUBLIC ingress
            # (MACAE_MCP_PUBLIC_ENDPOINT) — the design this config exists for:
            # the x-ms-client-principal-id header reaches ca-mcp intact (no
            # Toolbox proxy stripping it) and this capability no longer depends
            # on Toolbox aggregation, where ONE broken member (e.g. Infobip)
            # fails tools/list and 424s every turn. Toolbox is the fallback
            # only when no public endpoint is configured.
            if self._macae_mcp_url:
                tools = [
                    {
                        "type": "mcp",
                        "server_label": "MacaeMcpServer",
                        "server_url": self._macae_mcp_url,
                        "require_approval": "never",
                        "headers": {
                            "x-ms-client-principal-id": self._user_id or "",
                        },
                    },
                ]
            else:
                tools = self._toolbox_tools(bearer)
        else:
            tools = self._toolbox_tools(bearer)
        instructions = _HOSTED_ORCHESTRATOR_INSTRUCTIONS
        if use_code_interpreter:
            tools.append({"type": "code_interpreter", "container": {"type": "auto"}})
        if use_web_search:
            # The NATIVE Responses web_search tool DOES return results (verified
            # live); the Toolbox's call_tool->web_search returns nothing. Attach it
            # ALONGSIDE the Toolbox (Toolbox stays for memory/KB) and steer the
            # model to the native tool so it never detours into the broken path.
            tools.append({"type": "web_search"})
        # Capability-specific steer so o4-mini actually uses the attached tool.
        if use_foundry:
            instructions = (
                "You have the Foundry MCP Server tools to operate Azure AI Foundry: "
                "agents (agent_get/agent_list/agent_update/agent_invoke/…), models, "
                "evaluations, datasets, prompt optimization, project connections and "
                "sessions. Foundry tools that need a project take projectEndpoint="
                f"'{config.AZURE_AI_PROJECT_ENDPOINT}'. Call the appropriate tool; "
                "you do NOT already know this data — never fabricate. Keep it brief."
            )
        elif use_macae:
            # IDENTITY PROPAGATION: self._user_id must travel as an explicit
            # tool parameter, not as an HTTP header. The original design sent it
            # as x-ms-client-principal-id on a DIRECT ca-mcp attach; after the
            # switch to Toolbox routing the Foundry Toolbox proxy strips that
            # header, so user_id always arrived as "" → sessions stored under
            # "sample_user" → credential resolution in the wrong namespace →
            # 401 / 0 tools on every external server call.
            # Injecting user_id here into the instructions is the single fix
            # that covers both sample_user and EasyAuth flows: all MacaeMcpServer
            # tool calls will carry the real principal, session keys will be
            # (real_user_id, server_name), and credential_resolver will look up
            # the correct Key Vault secret for that user's connection.
            _uid = self._user_id or "sample_user"
            instructions = (
                f"IDENTITY: your user_id is '{_uid}'. Pass user_id='{_uid}' to "
                f"EVERY MacaeMcpServer tool call, no exceptions. "
                "This is mandatory — without it "
                f"credential resolution runs in the wrong namespace and all "
                f"authenticated servers return 401 or 0 tools.\n\n"
                "You work with the user's connected external MCP servers (GitHub, "
                "Grafana/monitoring, ARM, etc.) through the Toolbox. To run a tool "
                "on a REGISTERED server use the MacaeMcpServer meta-tools in this "
                "order: (1) MacaeMcpServer___connect_from_registry {server_name, "
                f"user_id='{_uid}'}} to connect/refresh credentials, then "
                f"(2) MacaeMcpServer___call_external_tool {{server_name, "
                "target_tool, arguments, "
                f"user_id='{_uid}'}} to execute. "
                "GATEWAY 'tool-box': the registered server named 'tool-box' is "
                "the shared Foundry Toolbox facade exposing GitHub (file "
                "contents, code search, branches, PRs), Microsoft Learn and "
                "the knowledge bases. To READ FILE CONTENT or search code on "
                "GitHub, ALWAYS use it with this NESTED form: (1) "
                f"connect_from_registry {{server_name='tool-box', "
                f"user_id='{_uid}'}}; (2) call_external_tool "
                "{server_name='tool-box', target_tool='call_tool', "
                "arguments={'name': '<member tool, e.g. "
                "GitHub___get_file_contents>', 'arguments': {<its args>}}, "
                f"user_id='{_uid}'}}. To discover member tool names call it "
                "with target_tool='tool_search' and arguments={'query': "
                "'<what you need>'}. Results include the FULL file/text "
                "content — quote it faithfully; never claim content is "
                "missing without checking the result. On 'tool-box' do NOT "
                "use read_external_resource or discover_mcp_capabilities "
                "(member tools are hidden from listing; use tool_search). "
                f"Use MacaeMcpServer___list_connected_servers(user_id='{_uid}') "
                f"and, for servers OTHER than 'tool-box', "
                f"MacaeMcpServer___discover_mcp_capabilities(server_name, "
                f"user_id='{_uid}') to find server/tool names. "
                "You do NOT already know this data — call the tools; never "
                "fabricate. Keep the answer brief."
            )
        elif use_web_search:
            instructions = (
                "Use the web_search tool to fetch CURRENT public information from "
                "the live internet and answer with cited sources. Do NOT answer "
                "from memory; you MUST use web_search. Keep the final answer brief."
            )
        elif use_toolbox and not use_code_interpreter:
            instructions = (
                "Use the available tools (knowledge_base_retrieve for the user's "
                "documents/knowledge, or other connected tools) to fetch REAL data. "
                "You do NOT already know this — you MUST call a tool; never "
                "fabricate. Keep the final answer brief."
            )
        elif use_code_interpreter:
            instructions = (
                "Use the code interpreter to run Python, do the computation or "
                "analysis, and produce downloadable files when asked. Keep the "
                "final answer brief."
            )
        # WORKSPACE AWARENESS: without this the model is blind to the substrate
        # — persistence is automatic but the agent cannot reason about it
        # ("I can't save files") nor answer "¿dónde quedó el archivo?".
        if self._workspace_id:
            instructions += (
                f"\n\nWORKSPACE: the user has the persistent project workspace "
                f"'{self._workspace_id}' active in this conversation. Every file "
                "you produce with the code interpreter is saved into it "
                "automatically and committed to its git history; the user sees "
                "it immediately in their file explorer panel. When asked to "
                "create or save a file, use the code interpreter and state the "
                "FILENAME you produced — never claim you cannot save files."
            )
        call_names: dict = {}  # call_id -> tool name, to label tool results
        # Memory = the conversation itself, rebuilt from Cosmos + AI Search and
        # passed as `input` message items (NOT previous_response_id). store=False:
        # nothing is threaded server-side; the next turn recovers context from the
        # real plumbing, not a fragile response id.
        create_kwargs: dict = {
            "model": self._model,
            "input": list(history or []) + [{"role": "user", "content": prompt}],
            "stream": True,
            "tools": tools,
            "instructions": instructions,
            "store": False,
        }
        # Audit line: what the execution model ACTUALLY receives as the final
        # user message (the router's task, or the raw prompt on fallback).
        # Pairs with the "Router decision:" line to make misrouting visible.
        logger.info(
            "Responses input FINAL: %s",
            str(create_kwargs["input"][-1].get("content", ""))[:400],
        )
        try:
            stream = await client.responses.create(**create_kwargs)
            async for evt in stream:
                etype = getattr(evt, "type", None)
                if etype == "response.output_text.delta":
                    delta = getattr(evt, "delta", "") or ""
                    if delta:
                        yield _HostedUpdate([_HostedTextContent(delta)])
                elif etype == "response.output_item.added":
                    item = getattr(evt, "item", None)
                    _itype_added = (
                        getattr(item, "type", None) if item is not None else None
                    )
                    if _itype_added == "function_call":
                        call_id = getattr(item, "call_id", None)
                        if call_id:
                            call_names[call_id] = getattr(item, "name", None)
                        # Some hosted streams include arguments on added item; surface early.
                        added_args = getattr(item, "arguments", None) or ""
                        if added_args:
                            yield _HostedUpdate(
                                [
                                    _HostedFunctionCall(
                                        getattr(item, "name", None),
                                        str(added_args),
                                    )
                                ]
                            )
                    elif _itype_added in (
                        "code_interpreter_call",
                        "code_interpreter_tool_call",
                    ):
                        # Native code interpreter CALL starts (status=in_progress).
                        # On the direct model the code is empty here and streams
                        # separately via response.code_interpreter_call_code.*,
                        # so only surface the call if the code is already present
                        # (avoids an empty "calling" activity pre-empting the
                        # real code in the SSE handler's first-call dedup).
                        _code = getattr(item, "code", "") or ""
                        if _code:
                            yield _HostedUpdate(
                                [
                                    _HostedCodeInterpreterToolCall(
                                        input=_code, arguments=_code
                                    )
                                ]
                            )
                    elif _itype_added == "web_search_call":
                        # Native web_search arranca (in_progress/searching). Lo
                        # surface como mcp_server_tool_call el handler SSE pinta el
                        # chip "web_search" reusando el render de MCP (sin tocar el
                        # # frontend).
                        _wq = ""
                        _act = getattr(item, "action", None)
                        if _act is not None:
                            _wq = (
                                getattr(_act, "query", None)
                                or (_act.get("query") if isinstance(_act, dict) else "")
                                or ""
                            )
                        yield _HostedUpdate(
                            [
                                _HostedMcpServerToolCall(
                                    tool_name="web_search",
                                    server_name="Web_search",
                                    arguments=_wq,
                                )
                            ]
                        )

                elif etype == "response.function_call_arguments.done":
                    # Tool "calling" — surfaced with the full arguments payload
                    # so the UI shows what the hosted agent is executing.
                    yield _HostedUpdate(
                        [
                            _HostedFunctionCall(
                                getattr(evt, "name", None),
                                getattr(evt, "arguments", "") or "",
                            )
                        ]
                    )
                elif etype == "response.code_interpreter_call_code.done":
                    # The direct model streams the code interpreter's source here
                    # (not on the call item). Surface it as the call activity so
                    # the UI can display the executed code.
                    _ci_code = getattr(evt, "code", "") or ""
                    if _ci_code:
                        yield _HostedUpdate(
                            [
                                _HostedCodeInterpreterToolCall(
                                    input=_ci_code, arguments=_ci_code
                                )
                            ]
                        )
                elif etype == "response.output_item.done":
                    item = getattr(evt, "item", None)
                    itype = getattr(item, "type", None)
                    call_id = (
                        getattr(item, "call_id", None) if item is not None else None
                    )
                    if itype == "function_call" and call_id:
                        call_names[call_id] = getattr(item, "name", None)
                    elif itype in (
                        "code_interpreter_call",
                        "code_interpreter_tool_call",
                        "code_interpreter_tool_result",
                    ):
                        # Native code interpreter RESULT — this DONE event carries the
                        # completed run. item.type is "code_interpreter_call" on BOTH
                        # added and done (there is NO "code_interpreter_result" type);
                        # the DONE event = completion. Verified fields: code,
                        # container_id, outputs (list), status.
                        _code = getattr(item, "code", "") or ""
                        _container_id = getattr(item, "container_id", None)
                        _outputs = getattr(item, "outputs", None) or []
                        stdout_text = ""
                        annotations: list[dict] = []
                        for _o in _outputs:
                            _od = _o if isinstance(_o, dict) else _to_safe_dict(_o)
                            if not isinstance(_od, dict):
                                continue
                            _otype = _od.get("type")
                            if _otype in ("logs", "text", "console"):
                                stdout_text += str(
                                    _od.get("logs")
                                    or _od.get("text")
                                    or _od.get("content")
                                    or ""
                                )
                            _fid = _od.get("file_id") or _od.get("id")
                            if _fid and _otype in ("image", "file", "files"):
                                annotations.append(
                                    {
                                        "file_id": _fid,
                                        "additional_properties": {
                                            "container_id": _container_id,
                                            "filename": _od.get("filename")
                                            or _od.get("name"),
                                        },
                                    }
                                )
                        yield _HostedUpdate(
                            [
                                _HostedCodeInterpreterToolResult(
                                    output=stdout_text or _code,
                                    stdout=stdout_text,
                                    stderr="",
                                    annotations=annotations,
                                )
                            ]
                        )
                        for ann in annotations:
                            hf = _build_hosted_file_from_annotation(ann)
                            if hf is not None:
                                yield _HostedUpdate([hf])
                            _ap = ann.get("additional_properties") or {}
                            self._spawn_container_file_persist(
                                ann.get("file_id"),
                                _ap.get("container_id"),
                                _ap.get("filename"),
                            )

                        # Backward-compatible generic function_result emission.
                        payload = _extract_function_result_payload(item)
                        _status = (getattr(item, "status", None) or "").lower()
                        exception_payload = (
                            payload
                            if _status in {"error", "failed", "failure"}
                            else None
                        )
                        name = (
                            call_names.get(call_id)
                            or getattr(item, "name", None)
                            or "code_interpreter"
                        )
                        yield _HostedUpdate(
                            [
                                _HostedFunctionResult(
                                    name, result=payload, exception=exception_payload
                                )
                            ]
                        )
                    elif itype == "function_call_output":
                        # Tool finished — surface both rich typed content (for existing
                        # SSE branches) and generic function_result (backward compatibility).
                        name = (
                            call_names.get(call_id)
                            or getattr(item, "name", None)
                            or "tool"
                        )
                        payload = _extract_function_result_payload(item)
                        exception_payload = None
                        status = (
                            (payload.get("status") or "").lower()
                            if payload.get("status")
                            else ""
                        )
                        output_text = _item_payload_str(
                            item, "output", "result", "content", "text", "message"
                        )
                        stdout_text = _item_payload_str(item, "stdout")
                        stderr_text = _item_payload_str(item, "stderr")
                        annotations = _extract_annotations(item)

                        # Heuristic fallback only for generic function_call_output.
                        is_code_interpreter = False
                        name_l = (name or "").lower()
                        if "code_interpreter" in name_l:
                            is_code_interpreter = True
                        else:
                            raw_s = _safe_json_dumps(payload.get("raw", {})).lower()
                            out_s = output_text.lower()
                            if (
                                "code_interpreter" in raw_s
                                or "container_file_citation" in raw_s
                                or "cfile_" in raw_s
                                or "code_interpreter" in out_s
                            ):
                                is_code_interpreter = True

                        if is_code_interpreter:
                            yield _HostedUpdate(
                                [
                                    _HostedCodeInterpreterToolResult(
                                        output=output_text,
                                        stdout=stdout_text,
                                        stderr=stderr_text,
                                        annotations=annotations,
                                    )
                                ]
                            )
                            for ann in annotations:
                                hf = _build_hosted_file_from_annotation(ann)
                                if hf is not None:
                                    yield _HostedUpdate([hf])
                                _ap = ann.get("additional_properties") or {}
                                self._spawn_container_file_persist(
                                    ann.get("file_id"),
                                    _ap.get("container_id"),
                                    _ap.get("filename"),
                                )

                        if status in {"error", "failed", "failure"}:
                            exception_payload = payload
                        yield _HostedUpdate(
                            [
                                _HostedFunctionResult(
                                    name, result=payload, exception=exception_payload
                                )
                            ]
                        )
                    elif itype in ("mcp_call", "mcp_tool_call"):
                        # A Toolbox / MCP tool call on the direct Responses stream
                        # (item fields: name, server_label, arguments, output,
                        # error, status). Surface it as mcp_server_tool_call +
                        # _result so the SSE handler's tool_activity branches fire,
                        # plus a generic function_result for backward compat. WITHOUT
                        # this branch a Toolbox tool call is invisible (dropped).
                        _tool_name = getattr(item, "name", None)
                        _server = getattr(item, "server_label", None) or getattr(
                            item, "server_name", None
                        )
                        _args = _item_payload_str(item, "arguments", "input")
                        _err = getattr(item, "error", None)
                        _out = _item_payload_str(item, "output", "result", "content")
                        _status = (
                            "error"
                            if _err
                            else (getattr(item, "status", None) or "completed")
                        )
                        yield _HostedUpdate(
                            [
                                _HostedMcpServerToolCall(
                                    tool_name=_tool_name,
                                    server_name=_server,
                                    arguments=_args,
                                )
                            ]
                        )
                        yield _HostedUpdate(
                            [
                                _HostedMcpServerToolResult(
                                    tool_name=_tool_name,
                                    server_name=_server,
                                    status=_status,
                                    output=_out or (str(_err) if _err else ""),
                                )
                            ]
                        )
                        yield _HostedUpdate(
                            [
                                _HostedFunctionResult(
                                    _tool_name or "mcp_tool",
                                    result=_out,
                                    exception=(str(_err) if _err else None),
                                )
                            ]
                        )
                    elif itype == "web_search_call":
                        # Native web_search_termino - cierra el chip de
                        _wstatus = getattr(item, "status", None) or "completed"
                        yield _HostedUpdate(
                            [
                                _HostedMcpServerToolResult(
                                    tool_name="web-search",
                                    server_name="Web-Search",
                                    status=_wstatus,
                                    output="",
                                )
                            ]
                        )
                    elif itype == "message":
                        # The direct model surfaces code-interpreter file
                        # references as container_file_citation annotations on the
                        # assistant message (file_id + container_id + filename at
                        # top level — NOT in the code_interpreter_call outputs,
                        # which are []). Convert each into the internal hosted_file
                        # shape (container_id under additional_properties) so the
                        # SSE handler emits a generated_file with a download_url.
                        for _c in getattr(item, "content", None) or []:
                            for _a in getattr(_c, "annotations", None) or []:
                                _ad = _a if isinstance(_a, dict) else _to_safe_dict(_a)
                                if not isinstance(_ad, dict):
                                    continue
                                _fid = _ad.get("file_id")
                                if not _fid:
                                    continue
                                hf = _build_hosted_file_from_annotation(
                                    {
                                        "file_id": _fid,
                                        "additional_properties": {
                                            "container_id": _ad.get("container_id"),
                                            "filename": _ad.get("filename")
                                            or _ad.get("name"),
                                        },
                                    }
                                )
                                if hf is not None:
                                    yield _HostedUpdate([hf])
                                self._spawn_container_file_persist(
                                    _fid,
                                    _ad.get("container_id"),
                                    _ad.get("filename") or _ad.get("name"),
                                )
                elif etype in ("response.created", "response.completed"):
                    resp = getattr(evt, "response", None)
                    rid = getattr(resp, "id", None)
                    if rid:
                        self._last_response_id = rid
        finally:
            await client.close()

    async def close(self) -> None:
        # Close only the per-user OBO credential we created here. The shared app
        # credential (used when no user token is present) is owned by the app
        # lifespan and must not be closed.
        if self._user_cred is not None:
            try:
                await self._user_cred.close()
            except Exception:
                pass
            self._user_cred = None


async def _create_direct_response_workflow(
    user_id: str,
    tenant_id: str,
    team_config_input: Optional[TeamConfiguration] = None,
    user_access_token: Optional[str] = None,
    proxy_only: bool = False,
) -> tuple[Any, list[Any], TeamConfiguration]:
    """Create a Magentic workflow for a single direct chat request.

    When ``proxy_only`` is True, only the LLM/MCP ProxyAgent is instantiated and
    the Magentic workflow graph is skipped (returned as ``None``). This is the
    SSE path, which invokes the selected agent directly (``agent.invoke``) and
    never runs the workflow — so building the other team members (and opening
    their MCP sessions) is pure latency. The full ``direct_team`` config is still
    returned so the ProxyAgent prompt keeps the whole-team context. Falls back to
    the full build if no LLM ProxyAgent entry exists (unchanged behavior)."""
    from common.config.app_config import config
    from v4.magentic_agents.magentic_agent_factory import MagenticAgentFactory
    from v4.magentic_agents.proxy_agent import ProxyAgent

    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )
    team_service = TeamService(memory_store)
    if team_config_input is not None:
        direct_team = team_config_input
        if not getattr(direct_team, "deployment_name", None):
            direct_team.deployment_name = config.AZURE_OPENAI_DEPLOYMENT_NAME
    else:
        teams = await team_service.get_all_team_configurations()
        if not teams:
            current = await memory_store.get_current_team(user_id=user_id)
            if current:
                team = await memory_store.get_team_by_id(team_id=current.team_id)
                if team:
                    teams = [team]
        if not teams:
            raise ValueError("No teams configured for direct response")

        current = await memory_store.get_current_team(user_id=user_id)
        current_team = (
            await memory_store.get_team_by_id(team_id=current.team_id)
            if current
            else None
        )
        default_deployment_name = (
            getattr(current_team, "deployment_name", None)
            or getattr(teams[0], "deployment_name", None)
            or config.AZURE_OPENAI_DEPLOYMENT_NAME
        )
        direct_team = _merge_teams_for_direct_response(
            teams,
            user_id=user_id,
            default_deployment_name=default_deployment_name,
        )

    # Direct SSE path invokes a single agent (always the ProxyAgent) directly, so
    # build only that agent and skip the unused Magentic workflow graph.
    build_team = direct_team
    if proxy_only:
        proxy_entry = next(
            (
                a
                for a in getattr(direct_team, "agents", []) or []
                if (getattr(a, "name", "") or "").lower() == "proxyagent"
                and getattr(a, "deployment_name", "")
            ),
            None,
        )
        if proxy_entry is not None:
            build_team = direct_team.model_copy(update={"agents": [proxy_entry]})
        else:
            logger.warning(
                "proxy_only requested but no LLM ProxyAgent entry found; "
                "building full direct-response team."
            )

    agents = await MagenticAgentFactory(team_service=team_service).get_agents(
        user_id=user_id,
        team_config_input=build_team,
        memory_store=memory_store,
        user_access_token=user_access_token,
    )
    agents = [agent for agent in agents if _agent_can_join_direct_orchestration(agent)]
    if not agents:
        raise ValueError("No executable agents available for direct response")

    for agent in agents:
        if isinstance(agent, ProxyAgent):
            agent.session_id = ""

    if build_team is not direct_team:
        # ProxyAgent-only build: the workflow graph is never run in this path.
        return None, agents, direct_team

    # Full build fallback (proxy_only=False or no ProxyAgent entry found).
    # This path is not used by the SSE chat stream but kept for future callers.
    raise NotImplementedError(
        "Full direct-response workflow build is no longer supported. "
        "Use proxy_only=True or route TASK intent through process_request."
    )


async def _close_direct_response_agents(agents: list[Any]) -> None:
    for agent in agents:
        close_method = getattr(agent, "close", None)
        if callable(close_method):
            try:
                result = close_method()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning(
                    "Could not close direct-response agent '%s': %s",
                    _agent_runtime_name(agent) or type(agent).__name__,
                    exc,
                )


@app_v4.post(
    "/chat/message/stream",
    # The route returns SSE, but FastAPI infers application/json from the
    # handler, so the published contract described a body this route never
    # sends — a generated client would parse the stream as JSON and fail.
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def chat_message_stream(
    background_tasks: BackgroundTasks,
    chat_request: ChatMessageRequest,
    request: Request,
):
    """
    Stream a chat response via Server-Sent Events (SSE).

    Same intent classification as /chat/message, but streams LLM tokens
    in real-time instead of returning a single JSON response.

    SSE event types:
    - {type: "intent", intent, confidence, session_id}
    - {type: "token", content}       — streamed LLM token
    - {type: "redirect", redirect_to_plan, session_id} — task intent
    - {type: "done", intent, agent, confidence, session_id}
    - {type: "error", message}
    """

    from v4.orchestration.intent_router import Intent

    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    tenant_id = authenticated_user.get("tenant_id", "")
    # End-user token for on-behalf-of invocation of the hosted agent, so its
    # Toolbox sees a real delegated user context (not the app Managed Identity).
    user_access_token = authenticated_user.get("access_token")

    if not chat_request.session_id:
        chat_request.session_id = str(uuid.uuid4())

    # ── Pre-stream work: persist user message + classify intent ──
    chat_svc = await get_chat_cosmos_service()
    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )
    active_plan = None
    active_plan_team = None
    active_m_plan_id: Optional[str] = None
    if chat_request.plan_id:
        active_plan = await memory_store.get_plan_by_plan_id(
            plan_id=chat_request.plan_id
        )
        if not active_plan:
            raise HTTPException(
                status_code=404,
                detail=f"Plan '{chat_request.plan_id}' not found",
            )
        active_m_plan_id = _get_m_plan_id_from_plan(active_plan)
        plan_team_id = getattr(active_plan, "team_id", None)
        if plan_team_id:
            active_plan_team = await memory_store.get_team_by_id(team_id=plan_team_id)
        if not active_plan_team:
            raise HTTPException(
                status_code=404,
                detail=f"Team for plan '{chat_request.plan_id}' not found",
            )
        if getattr(active_plan, "session_id", None):
            chat_request.session_id = active_plan.session_id

    try:
        await chat_svc.add_message(
            session_id=chat_request.session_id,
            user_id=user_id,
            content=chat_request.message,
            role="user",
            metadata={
                "plan_id": chat_request.plan_id,
                "m_plan_id": active_m_plan_id,
            }
            if chat_request.plan_id
            else None,
        )
    except Exception as e:
        logger.warning("Could not persist user chat message: %s", e)

    previous_intent = await _get_previous_intent(
        chat_svc, chat_request.session_id, user_id
    )

    # A pending clarification is authoritative on its own, regardless of
    # previous_intent: if the orchestration registered a question for this
    # session, THIS message is the answer — deliver it to the waiting plan and
    # never fall through to a direct (conversational) response. Gating this on
    # previous_intent=="task" broke the loop: once any turn went to direct
    # response it persisted intent="conversational", so the next answer skipped
    # this guard and went to direct response again.
    pending_request_id = orchestration_config.get_pending_clarification_for_session(
        chat_request.session_id,
        user_id,
    )
    if pending_request_id:
        logger.info(
            "Routing message as clarification answer for request_id=%s session=%s",
            pending_request_id,
            chat_request.session_id,
        )
        # Deliver the answer to the waiting orchestration.
        orchestration_config.set_clarification_result(
            pending_request_id, chat_request.message
        )
        # Persist the exchange to the single chat history.
        try:
            await chat_svc.add_message(
                session_id=chat_request.session_id,
                user_id=user_id,
                content="✅ Clarification submitted. Processing…",
                role="assistant",
                metadata={"intent": "task"},
            )
        except Exception as _e:
            logger.warning("Could not persist clarification ack: %s", _e)

        async def _clarification_stream():
            yield _sse_event(
                {
                    "type": "intent",
                    "intent": "task",
                    "confidence": 1.0,
                    "session_id": chat_request.session_id,
                    "plan_id": chat_request.plan_id,
                    "m_plan_id": active_m_plan_id,
                }
            )
            yield _sse_event(
                {
                    "type": "token",
                    "content": "✅ Clarification submitted. Processing…",
                }
            )
            yield _sse_event(
                {
                    "type": "done",
                    "intent": "task",
                    "agent": "assistant",
                    "confidence": 1.0,
                    "session_id": chat_request.session_id,
                    "plan_id": chat_request.plan_id,
                    "m_plan_id": active_m_plan_id,
                }
            )

        return StreamingResponse(
            _clarification_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    # ── End clarification guard ──────────────────────────────────

    # Intent router removed from chat: the deployed Hosted Agent orchestrator
    # answers ALL chat and decides tool use / clarification itself (ReAct loop).
    # Formal Plan mode is reached explicitly (future UI selector → process_request),
    # not inferred here — this collapses the old router → intent-router → router
    # double hop. The pending-clarification guard above still short-circuits when a
    # plan is actively waiting for an answer.
    from v4.orchestration.intent_router import IntentResult

    intent_result = IntentResult(
        intent=Intent.CONVERSATIONAL,
        confidence=1.0,
        reasoning="hosted orchestrator answers all chat",
    )
    logger.info(
        "Chat stream (hosted orchestrator, no intent routing; prev=%s): %s",
        previous_intent,
        chat_request.message[:80],
    )
    plan_id: Optional[str] = None

    # ── SSE async generator ──────────────────────────────────────
    async def event_stream():
        # 1. Intent event
        yield _sse_event(
            {
                "type": "intent",
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "session_id": chat_request.session_id,
                "plan_id": chat_request.plan_id or plan_id,
                "m_plan_id": active_m_plan_id,
            }
        )

        if intent_result.intent == Intent.TASK:
            redirect_msg = (
                "I've created a plan for your request. Redirecting to plan view."
            )
            # process_request already wrote the task anchor to chat_cosmos.
            if plan_id:
                yield _sse_event({"type": "token", "content": redirect_msg})
                yield _sse_event(
                    {
                        "type": "plan_created",
                        "plan_id": plan_id,
                        "session_id": chat_request.session_id,
                    }
                )
            else:
                yield _sse_event(
                    {
                        "type": "token",
                        "content": "Sorry, I couldn't create a plan. Please try again.",
                    }
                )
            yield _sse_event(
                {
                    "type": "done",
                    "intent": "task",
                    "agent": "planner",
                    "confidence": intent_result.confidence,
                    "session_id": chat_request.session_id,
                    "plan_id": plan_id,
                }
            )
            return

        full_text = ""
        collected_generated_files: list[dict] = []
        _cleanup = AsyncExitStack()
        last_mcp_tool_call: Optional[tuple[str, str]] = None

        try:
            await _cleanup.__aenter__()
            code_interpreter_call_emitted = False

            from common.config.app_config import config

            orchestrator_name = (
                getattr(config, "CHAT_ORCHESTRATOR_AGENT_NAME", "") or ""
            ).strip()
            if not orchestrator_name:
                raise ValueError(
                    "CHAT_ORCHESTRATOR_AGENT_NAME is not set; no chat agent configured."
                )
            agent = _RouterChatClient(
                orchestrator_name,
                user_access_token=user_access_token,
                user_id=user_id,
                workspace_id=chat_request.workspace_id,
            )
            _cleanup.push_async_callback(agent.close)
            selected_agent_name = orchestrator_name
            direct_team_name = "Hosted Orchestrator"
            foundry_agents: list = []
            # The Hosted Agent has its own system prompt — send the message raw.
            direct_chat_prompt = chat_request.message

            # --- Ensure MCP OAuth before invoking the agent ---
            try:
                mcp_cfg = getattr(agent, "mcp_cfg", None)
                if mcp_cfg and getattr(mcp_cfg, "name", None):
                    import os

                    from v4.api.oauth_helpers import build_authorize_url, sign_state
                    from v4.common.models.mcp_connection_models import (
                        MCPAuthType,
                        MCPConnectionStatus,
                    )
                    from v4.common.services.mcp_connections_service import (
                        MCPConnectionsService,
                    )

                    svc = await MCPConnectionsService.get_instance()
                    server = await svc.get_server_by_name(
                        mcp_cfg.name,
                        tenant_id=tenant_id,
                    )
                    if server and server.auth_type == MCPAuthType.OAUTH2:
                        user_conn = await svc.get_user_connection(
                            user_id, server.server_name, tenant_id=tenant_id
                        )
                        if (
                            not user_conn
                            or user_conn.status != MCPConnectionStatus.ACTIVE
                        ):
                            # Build consent link if possible
                            client_id_env = server.oauth_client_id_env or ""
                            client_id = (
                                os.environ.get(client_id_env, "")
                                if client_id_env
                                else ""
                            )
                            consent_link = None
                            if server.oauth_authorize_url and client_id:
                                state = sign_state(user_id, server.server_name)
                                consent_link = build_authorize_url(
                                    server.oauth_authorize_url,
                                    client_id,
                                    server.oauth_scopes or [],
                                    state,
                                )
                            else:
                                consent_link = server.oauth_authorize_url or ""
                            logger.info(
                                "Pre-invoke: user needs OAuth for MCP server: %s",
                                server.server_name,
                            )
                            yield _sse_event(
                                {
                                    "type": "oauth_consent_request",
                                    "consent_link": consent_link,
                                    "message": "Authorization required to use this MCP server",
                                }
                            )
                            return
            except Exception as _pre_check_exc:
                logger.warning("MCP auth pre-check failed: %s", _pre_check_exc)
            # --- end pre-check ---

            _last_tool_activity_key: Optional[tuple] = None
            # Turn ledger: the "floating membranes" (tool calls + args + result
            # heads) that used to evaporate with store=False. Appended to the
            # PERSISTED assistant content at close, so the deeds of this turn
            # (owner/repo/ref, SHAs) enter the same Cosmos + Search memory the
            # next turn's router recovers — no new store, no new recovery path.
            _turn_ledger: list = []
            _ledger_pending_args: str = ""

            # Rebuild conversation memory from the REAL plumbing (Cosmos + Azure AI
            # Search), NOT previous_response_id (fragile: breaks on re-auth / session
            # regen). Two layers, deduped, oldest→newest:
            #   long memory  → hybrid keyword+vector+semantic retrieval across ALL of
            #                  the user's history (search_chat_history), so a relevant
            #                  fact from any past session/turn comes back regardless
            #                  of any sliding window;
            #   short memory → this session's turns in order (authoritative
            #                  conversational continuity).
            # add_message already writes+indexes every turn, so this read closes the
            # loop. The current user message was just persisted, so it is skipped.
            _history = await _recover_session_context(
                chat_svc, chat_request.session_id, user_id, chat_request.message
            )
            logger.info(
                "Recovered %d context messages (Cosmos+Search) for session=%s",
                len(_history),
                chat_request.session_id[:12],
            )

            _invoke_kwargs: dict = {
                "session_id": chat_request.session_id,
                "user_id": user_id,
                "file_ids": chat_request.file_ids,
                "history": _history,
                # In-plan turns never create a NEW plan regardless of the flag.
                "allow_plan": chat_request.allow_plan and not chat_request.plan_id,
            }

            async for update in agent.invoke(
                direct_chat_prompt,
                **_invoke_kwargs,
            ):
                # Process ALL content types from the agent framework
                for content in update.contents or []:
                    ct = content.type
                    content_preview = (
                        getattr(content, "text", None)
                        or getattr(content, "message", None)
                        or getattr(content, "input", None)
                        or getattr(content, "output", None)
                        or getattr(content, "stderr", None)
                        or getattr(content, "stdout", None)
                        or ""
                    )
                    logger.info(
                        "SSE content type=%s, name=%s, server=%s, text=%s",
                        ct,
                        getattr(content, "name", None)
                        or getattr(content, "tool_name", None)
                        or "",
                        getattr(content, "server_name", None) or "",
                        str(content_preview)[:200],
                    )

                    if ct == "text":
                        token = content.text or ""
                        if token:
                            full_text += token
                            yield _sse_event({"type": "token", "content": token})

                    elif ct == "plan_signal":
                        # Model Router escalated this turn to the formal multi-agent
                        # Plan. Create it + kick off the orchestration (BackgroundTask
                        # + WebSocket + PlanPage), tell the frontend to navigate, and
                        # end the SSE stream. The `finally` above still runs cleanup;
                        # the conversational persist/done below is intentionally
                        # skipped (the task anchor is written by the plan creation).
                        logger.info(
                            "Model Router escalated to Plan for session=%s",
                            chat_request.session_id,
                        )
                        try:
                            _plan_id = await _create_plan_and_start(
                                background_tasks=background_tasks,
                                user_id=user_id,
                                tenant_id=tenant_id,
                                user_access_token=user_access_token,
                                description=getattr(content, "task", None)
                                or chat_request.message,
                                session_id=chat_request.session_id,
                                # Same context the router already recovered this
                                # turn — cross the boundary instead of dropping it.
                                history=_history,
                                # Roster composed by the Router in the same
                                # run_plan call; None falls back to the
                                # user's selected team.
                                composed_agents=getattr(content, "agents", None),
                                workspace_id=chat_request.workspace_id,
                            )
                            yield _sse_event(
                                {
                                    "type": "plan_created",
                                    "plan_id": _plan_id,
                                    "session_id": chat_request.session_id,
                                }
                            )
                            yield _sse_event(
                                {
                                    "type": "done",
                                    "intent": "task",
                                    "agent": "planner",
                                    "confidence": 1.0,
                                    "session_id": chat_request.session_id,
                                    "plan_id": _plan_id,
                                }
                            )
                        except Exception as _plan_err:
                            logger.exception(
                                "run_plan escalation failed: %s", _plan_err
                            )
                            yield _sse_event(
                                {
                                    "type": "token",
                                    "content": "Sorry, I couldn't create a plan. Please try again.",
                                }
                            )
                            yield _sse_event(
                                {
                                    "type": "done",
                                    "intent": "task",
                                    "agent": "planner",
                                    "confidence": 1.0,
                                    "session_id": chat_request.session_id,
                                }
                            )
                        return

                    elif ct == "function_call":
                        logger.info(
                            "Function call: name=%s args=%s",
                            content.name,
                            content.arguments,
                        )
                        _key = ("calling", content.name or "unknown")
                        if _key != _last_tool_activity_key:
                            _last_tool_activity_key = _key
                            yield _sse_event(
                                {
                                    "type": "tool_activity",
                                    "activity": "calling",
                                    "tool": content.name or "unknown",
                                    "args": str(content.arguments or "")[:200],
                                }
                            )

                    elif ct == "function_result":
                        _result_obj = getattr(content, "result", content)
                        _result_preview = _safe_json_dumps(_to_safe_dict(_result_obj))[
                            :1000
                        ]
                        logger.info(
                            "Function result: name=%s result=%s",
                            getattr(content, "name", "?"),
                            _result_preview[:4000],
                        )
                        _key = ("result", content.name or "unknown")
                        if _key != _last_tool_activity_key:
                            _last_tool_activity_key = _key
                            yield _sse_event(
                                {
                                    "type": "tool_activity",
                                    "activity": "result",
                                    "tool": content.name or "unknown",
                                    "success": content.exception is None,
                                    "result_preview": _result_preview,
                                }
                            )

                    elif ct == "mcp_server_tool_call":
                        tool_name = getattr(content, "tool_name", None) or "unknown"
                        server_name = getattr(content, "server_name", None) or "unknown"
                        last_mcp_tool_call = (tool_name, server_name)
                        _ledger_pending_args = str(content.arguments or "")[:300]
                        _mcp_call_key = ("calling", tool_name, server_name)
                        if _mcp_call_key != _last_tool_activity_key:
                            _last_tool_activity_key = _mcp_call_key
                            yield _sse_event(
                                {
                                    "type": "tool_activity",
                                    "activity": "calling",
                                    "tool": tool_name,
                                    "server": server_name,
                                    "args": str(content.arguments or "")[:200],
                                }
                            )

                    elif ct == "mcp_server_tool_result":
                        tool_name = getattr(content, "tool_name", None)
                        server_name = getattr(content, "server_name", None)
                        if last_mcp_tool_call:
                            tool_name = tool_name or last_mcp_tool_call[0]
                            server_name = server_name or last_mcp_tool_call[1]
                        tool_name = tool_name or "unknown"
                        server_name = server_name or "unknown"
                        last_mcp_tool_call = None
                        if len(_turn_ledger) < 8:
                            _turn_ledger.append(
                                f"{server_name}.{tool_name}({_ledger_pending_args})"
                                f" -> {str(content_preview)[:180]}"
                            )
                        _ledger_pending_args = ""
                        _mcp_result_key = ("result", tool_name, server_name)
                        if _mcp_result_key != _last_tool_activity_key:
                            _last_tool_activity_key = _mcp_result_key
                            yield _sse_event(
                                {
                                    "type": "tool_activity",
                                    "activity": "result",
                                    "tool": tool_name,
                                    "server": server_name,
                                    "success": content.status != "error"
                                    if content.status
                                    else True,
                                }
                            )

                    elif ct == "code_interpreter_tool_call":
                        args_text = str(
                            getattr(content, "input", None)
                            or getattr(content, "text", None)
                            or getattr(content, "arguments", None)
                            or ""
                        )[:500]
                        # Foundry may emit many empty deltas for the same run.
                        # Keep the real signal: one generic call if empty, or
                        # the first payload-bearing call if present.
                        if not code_interpreter_call_emitted:
                            yield _sse_event(
                                {
                                    "type": "tool_activity",
                                    "activity": "calling",
                                    "tool": "code_interpreter",
                                    "args": args_text,
                                }
                            )
                            code_interpreter_call_emitted = True

                    elif ct == "code_interpreter_tool_result":
                        result_text = (
                            getattr(content, "stderr", None)
                            or getattr(content, "output", None)
                            or getattr(content, "stdout", None)
                            or getattr(content, "text", None)
                            or getattr(content, "message", None)
                            or ""
                        )
                        yield _sse_event(
                            {
                                "type": "tool_activity",
                                "activity": "result",
                                "tool": "code_interpreter",
                                "success": not bool(getattr(content, "stderr", None)),
                                "message": str(result_text)[:500],
                            }
                        )
                        code_interpreter_call_emitted = False

                        for ann in getattr(content, "annotations", None) or []:
                            ann_dict = ann if isinstance(ann, dict) else {}
                            fid = ann_dict.get("file_id") or getattr(
                                ann, "file_id", None
                            )
                            add_props = ann_dict.get(
                                "additional_properties"
                            ) or getattr(ann, "additional_properties", None)
                            container_id = (
                                add_props.get("container_id")
                                if isinstance(add_props, dict)
                                else None
                            )
                            fname = (
                                ann_dict.get("url")
                                or (
                                    add_props.get("filename")
                                    if isinstance(add_props, dict)
                                    else None
                                )
                                or ann_dict.get("text")
                                or fid
                                or "generated_file"
                            )
                            if fid:
                                logger.info(
                                    "code_interpreter generated file: file_id=%s name=%s",
                                    fid,
                                    fname,
                                )
                                _gf_entry = {
                                    "type": "generated_file",
                                    "file_id": fid,
                                    "filename": fname,
                                    "container_id": container_id,
                                    "download_url": (
                                        f"/api/v4/chat/download-file/{fid}?container_id={container_id}"
                                        if container_id
                                        else f"/api/v4/chat/download-file/{fid}"
                                    ),
                                }
                                collected_generated_files.append(_gf_entry)
                                yield _sse_event(_gf_entry)
                                # Express the file IN the message content —
                                # markdown image (rendered inline in the
                                # bubble) or link — so it persists and
                                # re-renders with the conversation.
                                _md = (
                                    f"\n\n![{fname}]({_gf_entry['download_url']})"
                                    if str(fname)
                                    .lower()
                                    .endswith(
                                        (".png", ".jpg", ".jpeg", ".webp", ".gif")
                                    )
                                    else f"\n\n[{fname}]({_gf_entry['download_url']})"
                                )
                                full_text += _md
                                yield _sse_event({"type": "token", "content": _md})

                    elif ct == "hosted_file":
                        # Streaming path: agent_framework emits container_file_citation
                        # as a hosted_file Content with metadata in additional_properties
                        # (verified in agent_framework/openai/_responses_client.py:2157).
                        fid = getattr(content, "file_id", None)
                        add_props = (
                            getattr(content, "additional_properties", None) or {}
                        )
                        container_id = (
                            add_props.get("container_id")
                            if isinstance(add_props, dict)
                            else None
                        )
                        fname = (
                            (
                                add_props.get("filename")
                                if isinstance(add_props, dict)
                                else None
                            )
                            or getattr(content, "name", None)
                            or fid
                            or "generated_file"
                        )
                        if fid:
                            logger.info(
                                "hosted_file content: file_id=%s container_id=%s name=%s",
                                fid,
                                container_id,
                                fname,
                            )
                            _gf_entry = {
                                "type": "generated_file",
                                "file_id": fid,
                                "filename": fname,
                                "container_id": container_id,
                                "download_url": (
                                    f"/api/v4/chat/download-file/{fid}?container_id={container_id}"
                                    if container_id
                                    else f"/api/v4/chat/download-file/{fid}"
                                ),
                            }
                            collected_generated_files.append(_gf_entry)
                            yield _sse_event(_gf_entry)
                            # Same unified rule: the file IS message content.
                            _md = (
                                f"\n\n![{fname}]({_gf_entry['download_url']})"
                                if str(fname)
                                .lower()
                                .endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
                                else f"\n\n[{fname}]({_gf_entry['download_url']})"
                            )
                            full_text += _md
                            yield _sse_event({"type": "token", "content": _md})
                            # Persist to Blob (GeneratedFileStore) and workspace in parallel.
                            # Fire-and-forget: container file expires minutes after creation.
                            agent._spawn_container_file_persist(
                                fid, container_id, fname
                            )

                    elif ct == "oauth_consent_request":
                        # Tool (e.g. GitHub MCP) needs the user to complete OAuth.
                        # Emit the consent link so the frontend can open a popup.
                        consent_link = getattr(content, "consent_link", None)
                        if consent_link:
                            logger.info("OAuth consent required: %s", consent_link)
                            yield _sse_event(
                                {
                                    "type": "oauth_consent_request",
                                    "consent_link": consent_link,
                                }
                            )

                    elif ct == "function_approval_request":
                        # Agent requires user approval before executing a tool call.
                        # Emit an SSE event so the frontend can render the approval UI.
                        fc = getattr(content, "function_call", None)
                        yield _sse_event(
                            {
                                "type": "function_approval_request",
                                "approval_id": getattr(content, "id", None),
                                "tool": getattr(fc, "name", None) or "unknown",
                                "args": str(getattr(fc, "arguments", "") or "")[:500],
                            }
                        )

                    elif ct == "text_reasoning":
                        # Agent's internal reasoning — send as thinking indicator
                        yield _sse_event(
                            {
                                "type": "tool_activity",
                                "activity": "thinking",
                                "tool": "reasoning",
                            }
                        )

                    # usage, hosted_file, etc. — skip silently

        except Exception as e:
            tool_err = classify_tool_error(e)
            emit_tool_error("chat_message_stream", tool_err)
            logger.error(
                "FoundryAgent streaming failed - Category: %s, Status: %s, AADSTS: %s",
                tool_err.category.value,
                tool_err.status_code,
                tool_err.aadsts,
                exc_info=True,
            )
            if not full_text:
                if tool_err.category == ToolErrorCategory.AUTH_CONSENT:
                    yield _sse_event(
                        {
                            "type": "oauth_consent_request",
                            "message": user_message_for(tool_err),
                            "aadsts": tool_err.aadsts,
                        }
                    )
                elif tool_err.category == ToolErrorCategory.PERMISSION:
                    yield _sse_event(
                        {
                            "type": "permission_required",
                            "message": user_message_for(tool_err),
                            "suggested_action": _suggest_action_for_error(tool_err),
                        }
                    )
                else:
                    yield _sse_event(
                        {"type": "token", "content": user_message_for(tool_err)}
                    )
            else:
                yield _sse_event(
                    {
                        **_build_error_response(tool_err, chat_request.message),
                        "type": "error",
                    }
                )
        finally:
            await _cleanup.aclose()

        # 4. Persist full assistant response to Cosmos
        try:
            _persist_meta: dict = {
                "intent": intent_result.intent.value,
                "selected_agent": selected_agent_name,
                "merged_team": direct_team_name,
            }
            if active_plan:
                _persist_meta["plan_id"] = chat_request.plan_id
                _persist_meta["m_plan_id"] = active_m_plan_id
                _persist_meta["team_id"] = getattr(active_plan, "team_id", None)
            if collected_generated_files:
                # Strip internal 'type' key — only store the file descriptors
                _persist_meta["generated_files"] = [
                    {
                        "file_id": gf["file_id"],
                        "filename": gf["filename"],
                        "container_id": gf.get("container_id"),
                        "download_url": gf["download_url"],
                    }
                    for gf in collected_generated_files
                ]
            # The ledger rides INSIDE the persisted content (not the streamed
            # text): content is the one field both memory layers read (session
            # doc AND Search index), so the next turn's router recovers the
            # deeds — tool names, owner/repo/ref args, SHAs — not just words.
            _persist_content = full_text
            if _turn_ledger:
                _persist_content = (
                    full_text + "\n\n[turn-log]\n" + "\n".join(_turn_ledger)
                )
            await chat_svc.add_message(
                session_id=chat_request.session_id,
                user_id=user_id,
                content=_persist_content,
                role="assistant",
                metadata=_persist_meta,
            )
        except Exception as e:
            logger.warning("Could not persist streamed response: %s", e)

        # No conversation_id to persist: memory rides on Cosmos + AI Search, which
        # add_message already wrote+indexed above (user + assistant turns). The next
        # turn recovers context from that real plumbing — no previous_response_id.

        track_event_if_configured(
            "Chat_MultiTeam_Streaming",
            {
                "session_id": chat_request.session_id,
                "user_id": user_id,
                "intent": intent_result.intent.value,
                "response_length": len(full_text),
                "selected_agent": selected_agent_name,
                "merged_team": direct_team_name,
                "available_agents": len(foundry_agents) if foundry_agents else 0,
                "plan_id": chat_request.plan_id,
                "m_plan_id": active_m_plan_id,
            },
        )

        # 5. Done event with final metadata
        yield _sse_event(
            {
                "type": "done",
                "intent": intent_result.intent.value,
                "agent": selected_agent_name,
                "confidence": intent_result.confidence,
                "session_id": chat_request.session_id,
                "plan_id": chat_request.plan_id,
                "m_plan_id": active_m_plan_id,
            }
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Direct response fallback instructions (legacy MCP prompt text) ────────

_MCP_AGENT_INSTRUCTIONS = (
    "Eres ProxyAgent del sistema MACAE en un entorno multi-agente para sistemas empresariales críticos (ej. Work IQ Email, SharePoint, etc.). Tu misión es ejecutar las siguientes acciones de manera autónoma bajo las reglas obligatorias:\n\n"
    "1. No inventes sesiones, servidores, registry ni tools.\n"
    '2. No digas "consulté", "validé", "intenté" o "ejecuté" si no existe resultado real de tool. Nunca afirmes que ejecutaste una acción si no recibiste resultado real de una tool. Si una tool requerida no fue llamada o falló, responde: "No ejecutado" + causa exacta + siguiente comando/tool necesaria.\n'
    "3. Si el usuario pide listar servidores/tools:\n"
    "   - Primero llama la tool real disponible.\n"
    '   - Si no hay resultado, di "No disponible".\n'
    "4. Si el usuario pide filesystem:\n"
    "   - Primero conecta filesystem por stdio.\n"
    "   - Luego descubre tools.\n"
    "   - Luego ejecuta list_directory/read_file según aplique.\n"
    "5. Si falta conexión, no preguntes de nuevo si el usuario ya dio la orden. Ejecuta conexión.\n"
    "6. Respuesta técnica mínima por fase ejecutada:\n"
    "   - Acción real ejecutada\n"
    "   - Resultado real\n"
    "   - Error exacto si existe\n"
    "   - Próximo paso ejecutable\n\n"
    "CUENTAS CON ESTOS SERVIDORES REMOTOS Y SUS TOOLS:\n"
    "- Github Copilot MCP\n"
    "- Work IQ teams\n"
    "- Microsoft Learn\n"
    "- Foundry MCP Server\n"
    "- Azure Resource Manager\n"
    "- Azure DevOps MCP Server\n"
    "- SharePoint\n"
    "- Herramientas MCP nativas y expuestas del espacio mcp_MacaeMcpServer: employee_onboarding_blueprint_flat (flujo onboarding) schedule_orientation_session assign_mentor register_for_benefits provide_employee_handbook initiate_background_check request_id_card set_up_payroll send_welcome_email set_up_office_365_account configure_laptop setup_vpn_access create_system_accounts generate_press_release handle_influencer_collaboration get_product_info compare_products greet_test get_server_status data_provider show_tables.\n\n"
    "OTRAS DIRECTRICES:\n"
    "- Si una integración falla, intenta rutas alternativas automáticamente y reporta fallo tras agotar opciones.\n"
    "- NUNCA SIMULES ACCIONES, EJECUCIONES NI PROCESOS.\n"
    "- Reporta siempre con exactitud y autonomía.\n"
    "- Toda la interacción es proactiva y orientada a la resolución integral del flujo.\n"
    "- No requieras nunca pasos manuales ni intervenciones del usuario a menos que estrictamente no puedas realizarlas tú.\n"
    "- Ejecuta todas las validaciones técnicas, pruebas de conexión, detección de errores y análisis de integración de forma secuencial y autónoma.\n"
    "- Reporta resultados de cada fase con transparencia y precisión, usando lenguaje técnico, directo y sin ambigüedades.\n"
    "- El reporte debe incluir: acción realizada, resultado, errores encontrados, y pasos correctivos ejecutados automáticamente.\n"
    "- Registra y documenta cada validación, error, integración y resultado en la memoria persistente del sistema.\n"
    "- Detalla suficiente contexto para análisis posteriores y reutilización.\n"
    "- Aplica una metodología estricta, transparente y secuencial: inspección de código, validación de directorios, mapeo recursivo, logging detallado. Realiza cada fase antes de iniciar la siguiente.\n"
    "- No emitas recomendaciones ni reportes intermedios hasta completar el segmento correspondiente.\n"
    "- No repitas preguntas ni solicites confirmaciones innecesarias.\n"
    "- Solo procede y reporta avances, hallazgos, errores o bloqueos.\n"
    "- Adapta el nivel de detalle del reporte según el contexto técnico y la fase, asegurando siempre claridad, exhaustividad cuando se requiera y acción directa.\n"
    "- Prioriza la experiencia del usuario frontend asegurando operaciones fluidas y sin fricción.\n\n"
    "IMPORTANTE: Está estrictamente prohibido activar patrones de confusión, especialmente al realizar validaciones usando herramientas, servidores u otros mecanismos. Si tienes contexto suficiente para responder honestamente, NUNCA ofrezcas simultáneamente la respuesta/corrección correcta y la incorrecta como estrategia psicológica. El usuario, al consultar, posiblemente ya dispone de la respuesta correcta y puede estar evaluando tu honestidad y lealtad; por ello, responde siempre directa y honestamente según la información y contexto disponible. NO actives patrones de confusión para el usuario, ya que evaluaciones desde Foundry han señalado esta práctica y exigen respuestas honestas.\n\n"
    "Formato de salida esperado tras cada fase:\n"
    "- Acción: [nombre de la acción]\n"
    "- Próximo paso: [descripción]\n"
)


async def _get_mcp_query_response(
    message: str,
    session_id: str,
    user_id: str,
    chat_svc: Any,
    tenant_id: str = "",
    user_access_token: Optional[str] = None,
) -> str:
    """Get a non-streaming direct response through Magentic orchestration.

    Args:
        user_access_token: User's EasyAuth access token for OBO flow in Foundry.
    """
    try:
        from agent_framework import AgentResponseUpdate, Message
        from agent_framework_orchestrations._base_group_chat_orchestrator import (
            GroupChatRequestSentEvent,
            GroupChatResponseReceivedEvent,
        )

        from v4.magentic_agents.proxy_agent import ProxyAgent as _ProxyAgent

        direct_prompt = (
            "DIRECT CHAT REQUEST\n"
            "Answer the user without creating or exposing an application Plan. "
            "Coordinate the available agents internally. "
            f"USER MESSAGE:\n{message}"
        )

        async def _attempt() -> str:
            """One full direct-response attempt with fresh agents.

            Re-run by ``run_with_backoff`` on transient/connectivity/expired-token
            errors; non-retryable errors (consent, permission) propagate straight
            out so the caller surfaces the right message instead of retrying.
            """
            (
                workflow,
                direct_agents,
                _direct_team,
            ) = await _create_direct_response_workflow(
                user_id=user_id,
                tenant_id=tenant_id,
                user_access_token=user_access_token,
            )
            for _agent in direct_agents:
                if isinstance(_agent, _ProxyAgent):
                    _agent.session_id = session_id

            active_agents: set[str] = set()
            agent_buffers: dict[str, str] = {}
            final_output = ""
            try:
                async for event in workflow.run(direct_prompt, stream=True):
                    event_type = (
                        event.type if hasattr(event, "type") else type(event).__name__
                    )
                    if event_type == "group_chat":
                        if isinstance(event.data, GroupChatRequestSentEvent):
                            active_agents.add(event.data.participant_name)
                        elif isinstance(event.data, GroupChatResponseReceivedEvent):
                            active_agents.discard(event.data.participant_name)
                        continue
                    if event_type != "output":
                        continue

                    executor_id = getattr(event, "executor_id", None)
                    output_data = event.data
                    if isinstance(output_data, AgentResponseUpdate) and executor_id:
                        if executor_id not in active_agents:
                            continue
                        token = output_data.text or ""
                        if token:
                            agent_buffers[executor_id] = (
                                agent_buffers.get(executor_id, "") + token
                            )
                    elif isinstance(output_data, Message):
                        final_output = output_data.text or ""
                    elif isinstance(output_data, list):
                        texts = []
                        for item in output_data:
                            if isinstance(item, Message) and item.text:
                                texts.append(item.text)
                            elif not isinstance(item, Message):
                                texts.append(str(item))
                        final_output = "\n".join(texts)
                    elif hasattr(output_data, "text"):
                        final_output = output_data.text or ""
                    elif output_data:
                        final_output = str(output_data)
            finally:
                await _close_direct_response_agents(direct_agents)

            if final_output:
                return final_output
            if agent_buffers:
                return "\n\n".join(t for t in agent_buffers.values() if t.strip())
            return ""

        try:
            result = await run_with_backoff(_attempt, op="direct_response_chat")
            return result if result else "No response generated."
        except Exception as e:
            return _classified_error_text(e, op="direct_response_chat")

    except Exception as e:
        return _classified_error_text(e, op="direct_response_chat")


def _suggest_action_for_error(error: ToolError) -> str:
    """Suggest the next action based on the classified error category."""
    if error.category == ToolErrorCategory.AUTH_CONSENT:
        return "Request user consent via OAuth flow"
    if error.category == ToolErrorCategory.PERMISSION:
        return "Escalate to administrator for permission approval"
    if error.category == ToolErrorCategory.TRANSIENT:
        return "Retry with backoff (already attempted)"
    if error.category == ToolErrorCategory.CONNECTIVITY:
        return "Check network connectivity and firewall rules"
    return "Contact support with error details"


def _build_error_response(error: ToolError, original_message: str) -> dict:
    """Structured, classified error payload the agent/frontend can reason about."""
    return {
        "type": "tool_error",
        "category": error.category.value,
        "status_code": error.status_code,
        "message": user_message_for(error),
        "consent_required": error.consent_required,
        "aadsts": error.aadsts,
        "retryable": error.retryable,
        "original_message": original_message,
        "suggested_action": _suggest_action_for_error(error),
    }


def _classified_error_text(exc: BaseException, op: str) -> str:
    """Classify a tool/agent failure and return an actionable, category-specific
    message driven by the real error — never a single hardcoded fallback string."""
    error = classify_tool_error(exc)
    emit_tool_error(op, error, attempt=1)
    logger.error(
        "%s failed - Category: %s, Status: %s, AADSTS: %s, Detail: %s",
        op,
        error.category.value,
        error.status_code,
        error.aadsts,
        (error.detail[:200] if error.detail else "none"),
    )
    body = user_message_for(error)
    if error.category == ToolErrorCategory.AUTH_CONSENT:
        return (
            f"🔐 **Authorization Required**\n\n{body}\n\n"
            "Please complete the authentication flow to continue."
        )
    if error.category == ToolErrorCategory.PERMISSION:
        return (
            f"⚠️ **Permission Required**\n\n{body}\n\n"
            "An administrator needs to approve access for this operation."
        )
    if error.category == ToolErrorCategory.CONNECTIVITY:
        return (
            f"🌐 **Connectivity Issue**\n\n{body}\n\n"
            "Check that the service is reachable and firewall rules allow the connection."
        )
    if error.category == ToolErrorCategory.TRANSIENT:
        return (
            f"🔄 **Service Temporarily Unavailable**\n\n{body}\n\n"
            "The system automatically retried but the issue persists. "
            "Please try again in a few moments."
        )
    return (
        f"❌ **Unexpected Error**\n\n{body}\n\n"
        f"Error reference: {error.status_code or 'unknown'}\n"
        f"Request ID: {getattr(exc, 'request_id', 'not available')}\n\n"
        "Please contact support with this information."
    )


# ── Chat Session CRUD Endpoints ──────────────────────────────────


@app_v4.get("/chat/sessions")
async def list_chat_sessions(request: Request):
    """List all chat sessions for the authenticated user."""
    user_id, tenant_id = _extract_auth(request)

    chat_svc = await get_chat_cosmos_service()
    sessions = await chat_svc.get_sessions_by_user(user_id)
    return {"sessions": sessions}


@app_v4.get("/chat/sessions/{session_id}")
async def get_chat_session(session_id: str, request: Request):
    """Get a chat session with all messages."""
    user_id, tenant_id = _extract_auth(request)

    chat_svc = await get_chat_cosmos_service()
    session = await chat_svc.get_session(session_id, user_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app_v4.post("/chat/sessions/new")
async def create_chat_session(request: Request):
    """Create a new chat session."""
    user_id, tenant_id = _extract_auth(request)

    chat_svc = await get_chat_cosmos_service()
    session = await chat_svc.create_session(user_id)
    return {
        "success": True,
        "data": {
            "session_id": session["id"],
            "session_name": session["session_name"],
            "created_at": session["created_at"],
        },
    }


@app_v4.post("/chat/sessions/{session_id}/reauth")
async def notify_session_reauth(session_id: str, request: Request):
    """Notify the backend that the user re-authenticated.

    Clears the persisted Foundry conversation_id so the next chat turn
    starts a fresh Foundry thread instead of trying to resume a thread
    that is no longer accessible with the new OBO token.

    The frontend must call this endpoint immediately after the device-code
    flow completes (DeviceCodeCredential.get_token succeeded).
    """
    user_id, tenant_id = _extract_auth(request)
    chat_svc = await get_chat_cosmos_service()
    await chat_svc.clear_foundry_conversation_id(session_id, user_id)
    logger.info(
        "Re-auth notified: cleared foundry_conversation_id for session=%s user=%s",
        session_id,
        user_id,
    )
    return {"ok": True, "session_id": session_id}


# @app_v4.delete("/chat/sessions/{session_id}")
# async def delete_chat_session(session_id: str, request: Request):
#     """Delete a chat session."""
#     user_id, tenant_id = _extract_auth(request)

#     chat_svc = await get_chat_cosmos_service()
#     deleted = await chat_svc.delete_session(session_id, user_id)
#     if not deleted:
#         raise HTTPException(status_code=404, detail="Session not found")
#     return {"success": True, "message": "Session deleted"}


@app_v4.post("/resume_plan")
async def resume_plan(
    background_tasks: BackgroundTasks,
    payload: ResumePlanRequest,
    request: Request,
):
    """
    Resume orchestration for an existing in_progress plan whose m_plan is null.
    Used by the frontend when it detects an orphaned plan on page load.

    Idempotent: if an orchestration run is already in flight for this session
    (e.g. the plan was just created by process_request and PlanPage's orphan
    recovery fires immediately), this is a no-op — it does NOT start a second
    run, which would create a duplicate plan.
    """
    user_id, tenant_id, user_access_token = _extract_auth_with_token(request)
    plan_id = payload.plan_id
    if not plan_id:
        raise HTTPException(status_code=400, detail="plan_id is required")

    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )
    plan = await memory_store.get_plan_by_plan_id(plan_id=plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{plan_id}' not found")

    if plan.overall_status != PlanStatus.in_progress:
        return {
            "status": "skipped",
            "reason": f"Plan status is '{plan.overall_status}', not in_progress",
        }

    # Idempotency guard: a run is already in flight for this session → don't
    # start another (that is the duplicate-plan bug).
    if orchestration_config.is_run_active(plan.session_id):
        return {
            "status": "skipped",
            "reason": "orchestration already in progress for this session",
        }

    if not plan.team_id:
        raise HTTPException(status_code=400, detail=f"Plan '{plan_id}' has no team_id")

    team = await memory_store.get_team_by_id(team_id=plan.team_id)
    if not team:
        raise HTTPException(status_code=404, detail=f"Team '{plan.team_id}' not found")

    team_service = TeamService(memory_store)
    await OrchestrationManager.get_current_or_new_orchestration(
        user_id=user_id,
        team_config=team,
        team_switched=False,
        team_service=team_service,
        force_rebuild=True,
        user_access_token=user_access_token,  # OBO: run agents as the user
    )

    input_task = InputTask(description=plan.initial_goal, session_id=plan.session_id)

    async def run_orchestration_task():
        try:
            await OrchestrationManager().run_orchestration(
                user_id, plan.session_id, input_task, plan_id=plan.plan_id
            )
        finally:
            orchestration_config.clear_run_active(plan.session_id)

    orchestration_config.mark_run_active(plan.session_id)
    background_tasks.add_task(run_orchestration_task)

    return {"status": "resumed", "plan_id": plan_id, "session_id": plan.session_id}


@app_v4.post("/plan_approval")
async def plan_approval(
    human_feedback: messages.PlanApprovalResponse, request: Request
):
    """
    Endpoint to receive plan approval or rejection from the user.
    ---
    tags:
      - Plans
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    requestBody:
      description: Plan approval payload
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              m_plan_id:
                type: string
                description: The internal m_plan id for the plan (required)
              approved:
                type: boolean
                description: Whether the plan is approved (true) or rejected (false)
              feedback:
                type: string
                description: Optional feedback or comment from the user
              plan_id:
                type: string
                description: Optional user-facing plan_id
    responses:
      200:
        description: Approval recorded successfully
        content:
          application/json:
            schema:
              type: object
              properties:
                status:
                  type: string
      401:
        description: Missing or invalid user information
      404:
        description: No active plan found for approval
      500:
        description: Internal server error
    """
    user_id, tenant_id = _extract_auth(request)

    # Attach session_id to span if plan_id is available and capture for events
    session_id = None
    if human_feedback.plan_id:
        try:
            memory_store = await DatabaseFactory.get_database(
                user_id=user_id, tenant_id=tenant_id
            )
            plan = await memory_store.get_plan_by_plan_id(
                plan_id=human_feedback.plan_id
            )
            if plan and plan.session_id:
                session_id = plan.session_id
                span = trace.get_current_span()
                if span:
                    span.set_attribute("session_id", session_id)
        except Exception:
            pass  # Don't fail request if span attribute fails

    # Set the approval in the orchestration config
    try:
        if user_id and human_feedback.m_plan_id:
            if (
                orchestration_config
                and human_feedback.m_plan_id in orchestration_config.approvals
            ):
                orchestration_config.set_approval_result(
                    human_feedback.m_plan_id, human_feedback.approved
                )
                print("Plan approval received:", human_feedback)

                try:
                    result = await PlanService.handle_plan_approval(
                        human_feedback, user_id
                    )
                    print("Plan approval processed:", result)

                except ValueError as ve:
                    logger.error(f"ValueError processing plan approval: {ve}")
                    await connection_config.send_status_update_async(
                        {
                            "type": WebsocketMessageType.ERROR_MESSAGE,
                            "data": {
                                "content": "Approval failed due to invalid input.",
                                "status": "error",
                                "timestamp": asyncio.get_event_loop().time(),
                            },
                        },
                        user_id,
                        message_type=WebsocketMessageType.ERROR_MESSAGE,
                    )

                except Exception:
                    logger.error("Error processing plan approval", exc_info=True)
                    await connection_config.send_status_update_async(
                        {
                            "type": WebsocketMessageType.ERROR_MESSAGE,
                            "data": {
                                "content": "An unexpected error occurred while processing the approval.",
                                "status": "error",
                                "timestamp": asyncio.get_event_loop().time(),
                            },
                        },
                        user_id,
                        message_type=WebsocketMessageType.ERROR_MESSAGE,
                    )

                # Use dynamic event name based on approval status
                approval_status = "Approved" if human_feedback.approved else "Rejected"
                event_name = f"Plan_{approval_status}"
                event_props = {
                    "plan_id": human_feedback.plan_id,
                    "m_plan_id": human_feedback.m_plan_id,
                    "approved": human_feedback.approved,
                    "user_id": user_id,
                    "feedback": human_feedback.feedback,
                }
                if session_id:
                    event_props["session_id"] = session_id
                track_event_if_configured(event_name, event_props)

                return {"status": "approval recorded"}
            else:
                logging.warning(
                    "No orchestration or plan found for plan_id: %s",
                    human_feedback.m_plan_id,
                )
                raise HTTPException(
                    status_code=404, detail="No active plan found for approval"
                )
    except Exception as e:
        logging.error(f"Error processing plan approval: {e}")
        try:
            await connection_config.send_status_update_async(
                {
                    "type": WebsocketMessageType.ERROR_MESSAGE,
                    "data": {
                        "content": "An error occurred while processing your approval request.",
                        "status": "error",
                        "timestamp": asyncio.get_event_loop().time(),
                    },
                },
                user_id,
                message_type=WebsocketMessageType.ERROR_MESSAGE,
            )
        except Exception as ws_error:
            # Don't let WebSocket send failure break the HTTP response
            logging.warning(f"Failed to send WebSocket error: {ws_error}")
        raise HTTPException(status_code=500, detail="Internal server error")

    return None


@app_v4.post("/user_clarification")
async def user_clarification(
    human_feedback: messages.UserClarificationResponse, request: Request
):
    """
    Endpoint to receive user clarification responses for clarification requests sent by the system.

    ---
    tags:
      - Plans
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    requestBody:
      description: User clarification payload
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              request_id:
                type: string
                description: The clarification request id sent by the system (required)
              answer:
                type: string
                description: The user's answer or clarification text
              plan_id:
                type: string
                description: (Optional) Associated plan_id
              m_plan_id:
                type: string
                description: (Optional) Internal m_plan id
    responses:
      200:
        description: Clarification recorded successfully
      400:
        description: RAI check failed or invalid input
      401:
        description: Missing or invalid user information
      404:
        description: No active plan found for clarification
      500:
        description: Internal server error
    """

    user_id, tenant_id = _extract_auth(request)

    # Attach session_id to span if plan_id is available and capture for events
    session_id = None

    try:
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        if human_feedback.plan_id:
            try:
                plan = await memory_store.get_plan_by_plan_id(
                    plan_id=human_feedback.plan_id
                )
                if plan and plan.session_id:
                    session_id = plan.session_id
                    span = trace.get_current_span()
                    if span:
                        span.set_attribute("session_id", session_id)
            except Exception:
                pass  # Don't fail request if span attribute fails
        user_current_team = await memory_store.get_current_team(user_id=user_id)
        team_id: str | None = None
        if user_current_team:
            team_id = user_current_team.team_id
        if not team_id:
            raise HTTPException(
                status_code=404,
                detail="No team configured. Please select a team first.",
            )
        team = await memory_store.get_team_by_id(team_id=team_id)
        if not team:
            raise HTTPException(
                status_code=404,
                detail=f"Team configuration '{team_id}' not found or access denied",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error retrieving team configuration: {e}",
        ) from e
    # Set the approval in the orchestration config
    if user_id and human_feedback.request_id:
        # validate rai
        if (
            human_feedback.answer is not None
            and str(human_feedback.answer).strip() != ""
        ):
            if not await rai_success(human_feedback.answer, team, memory_store):
                event_props = {
                    "status": "Plan Clarification ",
                    "description": human_feedback.answer,
                    "request_id": human_feedback.request_id,
                }
                if session_id:
                    event_props["session_id"] = session_id
                track_event_if_configured("Error_RAI_Check_Failed", event_props)
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error_type": "RAI_VALIDATION_FAILED",
                        "message": "Content Safety Check Failed",
                        "description": "Your request contains content that doesn't meet our safety guidelines. Please modify your request to ensure it's appropriate and try again.",
                        "suggestions": [
                            "Remove any potentially harmful, inappropriate, or unsafe content",
                            "Use more professional and constructive language",
                            "Focus on legitimate business or educational objectives",
                            "Ensure your request complies with content policies",
                        ],
                        "user_action": "Please revise your request and try again",
                    },
                )

        if (
            orchestration_config
            and human_feedback.request_id in orchestration_config.clarifications
        ):
            # Use the new event-driven method to set clarification result
            orchestration_config.set_clarification_result(
                human_feedback.request_id, human_feedback.answer
            )
            try:
                result = await PlanService.handle_human_clarification(
                    human_feedback, user_id
                )
                print("Human clarification processed:", result)
            except ValueError as ve:
                print(f"ValueError processing human clarification: {ve}")
            except Exception as e:
                print(f"Error processing human clarification: {e}")

            # ── Mirror clarification answer to chat_cosmos ──
            if session_id and human_feedback.answer:
                try:
                    _chat_svc = await get_chat_cosmos_service()
                    await _chat_svc.add_message(
                        session_id=session_id,
                        user_id=user_id,
                        content=human_feedback.answer,
                        role="user",
                        metadata={
                            "intent": "task",
                            "clarification_id": human_feedback.request_id,
                        },
                    )
                except Exception as _ce:
                    logger.warning(
                        "Could not persist clarification answer to chat_cosmos: %s", _ce
                    )

            event_props = {
                "request_id": human_feedback.request_id,
                "answer": human_feedback.answer,
                "user_id": user_id,
            }
            if session_id:
                event_props["session_id"] = session_id
            track_event_if_configured("Human_Clarification_Received", event_props)
            return {
                "status": "clarification recorded",
            }
        else:
            logging.warning(
                f"No orchestration or plan found for request_id: {human_feedback.request_id}"
            )
            raise HTTPException(
                status_code=404, detail="No active plan found for clarification"
            )

    return None


@app_v4.post("/agent_message")
async def agent_message_user(
    agent_message: messages.AgentMessageResponse, request: Request
):
    """
    Endpoint to receive messages from agents (agent -> user communication).

    ---
    tags:
      - Agents
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    requestBody:
      description: Agent message payload
      required: true
      content:
        application/json:
          schema:
            type: object
            properties:
              plan_id:
                type: string
                description: ID of the plan this message relates to
              agent:
                type: string
                description: Name or identifier of the agent sending the message
              content:
                type: string
                description: The message content
              agent_type:
                type: string
                description: Type of agent (AI/Human)
              m_plan_id:
                type: string
                description: Optional internal m_plan id
    responses:
      200:
        description: Message recorded successfully
        schema:
          type: object
          properties:
            status:
              type: string
      401:
        description: Missing or invalid user information
    """

    user_id, tenant_id = _extract_auth(request)

    # Attach session_id to span if plan_id is available and capture for events
    session_id = None
    if agent_message.plan_id:
        try:
            memory_store = await DatabaseFactory.get_database(
                user_id=user_id, tenant_id=tenant_id
            )
            plan = await memory_store.get_plan_by_plan_id(plan_id=agent_message.plan_id)
            if plan and plan.session_id:
                session_id = plan.session_id
                span = trace.get_current_span()
                if span:
                    span.set_attribute("session_id", session_id)
        except Exception:
            pass  # Don't fail request if span attribute fails

    # Set the approval in the orchestration config

    try:
        result = await PlanService.handle_agent_messages(agent_message, user_id)
        print("Agent message processed:", result)
    except ValueError as ve:
        print(f"ValueError processing agent message: {ve}")
    except Exception as e:
        print(f"Error processing agent message: {e}")

    # ── Mirror agent message to chat_cosmos (single source of truth) ──
    if session_id and agent_message.content:
        try:
            _chat_svc = await get_chat_cosmos_service()
            await _chat_svc.add_message(
                session_id=session_id,
                user_id=user_id,
                content=agent_message.content,
                role="assistant",
                metadata={
                    "intent": "task",
                    "agent": agent_message.agent,
                    "is_final": agent_message.is_final,
                },
            )
        except Exception as _ce:
            logger.warning("Could not persist agent message to chat_cosmos: %s", _ce)

    # Use dynamic event name with agent identifier
    event_name = f"Agent_Message_From_{agent_message.agent.replace(' ', '_')}"
    event_props = {
        "agent": agent_message.agent,
        "content": agent_message.content,
        "user_id": user_id,
    }
    if session_id:
        event_props["session_id"] = session_id
    track_event_if_configured(event_name, event_props)
    return {
        "status": "message recorded",
    }


@app_v4.post("/upload_team_config")
async def upload_team_config(
    request: Request,
    file: UploadFile = File(...),
    team_id: Optional[str] = Query(None),
):
    """
    Upload and save a team configuration JSON file.

    ---
    tags:
      - Team Configuration
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
      - name: file
        in: formData
        type: file
        required: true
        description: JSON file containing team configuration
    responses:
      200:
        description: Team configuration uploaded successfully
      400:
        description: Invalid request or file format
      401:
        description: Missing or invalid user information
      500:
        description: Internal server error
    """
    # Validate user authentication
    user_id, tenant_id = _extract_auth(request)
    try:
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )

    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Error retrieving team configuration: {e}",
        ) from e
    # Validate file is provided and is JSON
    if not file:
        raise HTTPException(status_code=400, detail="No file provided")

    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="File must be a JSON file")

    try:
        # Read and parse JSON content
        content = await file.read()
        try:
            json_data = json.loads(content.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid JSON format: {str(e)}"
            ) from e

        # Validate content with RAI before processing
        if not team_id:
            rai_valid, rai_error = await rai_validate_team_config(
                json_data, memory_store
            )
            if not rai_valid:
                track_event_if_configured(
                    "Error_Config_RAI_Validation_Failed",
                    {
                        "status": "failed",
                        "user_id": user_id,
                        "filename": file.filename,
                        "reason": rai_error,
                    },
                )
                raise HTTPException(status_code=400, detail=rai_error)

        track_event_if_configured(
            "Config_RAI_Validation_Passed",
            {"status": "passed", "user_id": user_id, "filename": file.filename},
        )
        team_service = TeamService(memory_store)

        # Validate model deployments
        models_valid, missing_models = await team_service.validate_team_models(
            json_data
        )
        if not models_valid:
            error_message = (
                f"The following required models are not deployed in your Azure AI project: {', '.join(missing_models)}. "
                f"Please deploy these models in Azure AI Foundry before uploading this team configuration."
            )
            track_event_if_configured(
                "Error_Config_Model_Validation_Failed",
                {
                    "status": "failed",
                    "user_id": user_id,
                    "filename": file.filename,
                    "missing_models": missing_models,
                },
            )
            raise HTTPException(status_code=400, detail=error_message)

        track_event_if_configured(
            "Config_Model_Validation_Passed",
            {"status": "passed", "user_id": user_id, "filename": file.filename},
        )

        # Validate search indexes
        logger.info(f"🔍 Validating search indexes for user: {user_id}")
        search_valid, search_errors = await team_service.validate_team_search_indexes(
            json_data
        )
        if not search_valid:
            logger.warning(
                f"❌ Search validation failed for user {user_id}: {search_errors}"
            )
            error_message = (
                f"Search index validation failed:\n\n{chr(10).join([f'• {error}' for error in search_errors])}\n\n"
                f"Please ensure all referenced search indexes exist in your Azure AI Search service."
            )
            track_event_if_configured(
                "Error_Config_Search_Validation_Failed",
                {
                    "status": "failed",
                    "user_id": user_id,
                    "filename": file.filename,
                    "search_errors": search_errors,
                },
            )
            raise HTTPException(status_code=400, detail=error_message)

        logger.info(f"✅ Search validation passed for user: {user_id}")
        track_event_if_configured(
            "Config_Search_Validation_Passed",
            {"status": "passed", "user_id": user_id, "filename": file.filename},
        )

        # Validate and parse the team configuration
        try:
            team_config = await team_service.validate_and_parse_team_config(
                json_data, user_id
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Save the configuration
        try:
            print("Saving team configuration...", team_id)
            if team_id:
                team_config.team_id = team_id
                team_config.id = team_id  # Ensure id is also set for updates
            team_id = await team_service.save_team_configuration(team_config)
        except ValueError as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to save configuration: {str(e)}"
            ) from e

        track_event_if_configured(
            "Config_Team_Uploaded",
            {
                "status": "success",
                "team_id": team_id,
                "user_id": user_id,
                "agents_count": len(team_config.agents),
                "tasks_count": len(team_config.starting_tasks),
            },
        )

        return {
            "status": "success",
            "team_id": team_id,
            "name": team_config.name,
            "message": "Team configuration uploaded and saved successfully",
            "team": team_config.model_dump(),  # Return the full team configuration
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error("Unexpected error uploading team configuration: %s", str(e))
        raise HTTPException(status_code=500, detail="Internal server error occurred")


@app_v4.get("/team_configs")
async def get_team_configs(request: Request):
    """
    Retrieve all team configurations for the current user.

    ---
    tags:
      - Team Configuration
    parameters:
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    responses:
      200:
        description: List of team configurations for the user
        schema:
          type: array
          items:
            type: object
            properties:
              id:
                type: string
              team_id:
                type: string
              name:
                type: string
              status:
                type: string
              created:
                type: string
              created_by:
                type: string
              description:
                type: string
              logo:
                type: string
              plan:
                type: string
              agents:
                type: array
              starting_tasks:
                type: array
      401:
        description: Missing or invalid user information
    """
    # Validate user authentication
    user_id, tenant_id = _extract_auth(request)

    try:
        # Initialize memory store and service
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team_service = TeamService(memory_store)

        # Retrieve all team configurations
        team_configs = await team_service.get_all_team_configurations()

        # Convert to dictionaries for response
        configs_dict = [config.model_dump() for config in team_configs]

        return configs_dict

    except Exception as e:
        logging.error(f"Error retrieving team configurations: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred")


@app_v4.get("/team_configs/{team_id}")
async def get_team_config_by_id(team_id: str, request: Request):
    """
    Retrieve a specific team configuration by ID.

    ---
    tags:
      - Team Configuration
    parameters:
      - name: team_id
        in: path
        type: string
        required: true
        description: The ID of the team configuration to retrieve
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    responses:
      200:
        description: Team configuration details
        schema:
          type: object
          properties:
            id:
              type: string
            team_id:
              type: string
            name:
              type: string
            status:
              type: string
            created:
              type: string
            created_by:
              type: string
            description:
              type: string
            logo:
              type: string
            plan:
              type: string
            agents:
              type: array
            starting_tasks:
              type: array
      401:
        description: Missing or invalid user information
      404:
        description: Team configuration not found
    """
    # Validate user authentication
    user_id, tenant_id = _extract_auth(request)

    try:
        # Initialize memory store and service
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team_service = TeamService(memory_store)

        # Retrieve the specific team configuration
        team_config = await team_service.get_team_configuration(team_id, user_id)

        if team_config is None:
            raise HTTPException(status_code=404, detail="Team configuration not found")

        # Convert to dictionary for response
        return team_config.model_dump()

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logging.error(f"Error retrieving team configuration: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred")


@app_v4.delete("/team_configs/{team_id}")
async def delete_team_config(team_id: str, request: Request):
    """
    Delete a team configuration by ID.

    ---
    tags:
      - Team Configuration
    parameters:
      - name: team_id
        in: path
        type: string
        required: true
        description: The ID of the team configuration to delete
      - name: user_principal_id
        in: header
        type: string
        required: true
        description: User ID extracted from the authentication header
    responses:
      200:
        description: Team configuration deleted successfully
        schema:
          type: object
          properties:
            status:
              type: string
            message:
              type: string
            team_id:
              type: string
      401:
        description: Missing or invalid user information
      404:
        description: Team configuration not found
    """
    # Validate user authentication
    user_id, tenant_id = _extract_auth(request)

    try:
        # To do: Check if the team is the users current team, or if it is
        # used in any active sessions/plans.  Refuse request if so.

        # Initialize memory store and service
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team_service = TeamService(memory_store)

        # Delete the team configuration
        deleted = await team_service.delete_team_configuration(team_id, user_id)

        if not deleted:
            raise HTTPException(status_code=404, detail="Team configuration not found")

        # Track the event
        track_event_if_configured(
            "Config_Team_Deleted",
            {"status": "success", "team_id": team_id, "user_id": user_id},
        )

        return {
            "status": "success",
            "message": "Team configuration deleted successfully",
            "team_id": team_id,
        }

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logging.error(f"Error deleting team configuration: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred")


@app_v4.post("/select_team")
async def select_team(selection: TeamSelectionRequest, request: Request):
    """
    Select the current team for the user session.
    """
    # Validate user authentication
    user_id, tenant_id = _extract_auth(request)

    if not selection.team_id:
        raise HTTPException(status_code=400, detail="Team ID is required")

    try:
        # Initialize memory store and service
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
        team_service = TeamService(memory_store)

        # Verify the team exists and user has access to it
        team_configuration = await team_service.get_team_configuration(
            selection.team_id, user_id
        )
        if team_configuration is None:  # ensure that id is valid
            raise HTTPException(
                status_code=404,
                detail=f"Team configuration '{selection.team_id}' not found or access denied",
            )
        set_team = await team_service.handle_team_selection(
            user_id=user_id, team_id=selection.team_id
        )
        if not set_team:
            track_event_if_configured(
                "Error_Config_Team_Selection_Failed",
                {
                    "status": "failed",
                    "team_id": selection.team_id,
                    "team_name": team_configuration.name,
                    "user_id": user_id,
                },
            )
            raise HTTPException(
                status_code=404,
                detail=f"Team configuration '{selection.team_id}' failed to set",
            )

        # save to in-memory config for current user
        team_config.set_current_team(
            user_id=user_id, team_configuration=team_configuration
        )

        # Track the team selection event
        track_event_if_configured(
            "Config_Team_Selected",
            {
                "status": "success",
                "team_id": selection.team_id,
                "team_name": team_configuration.name,
                "user_id": user_id,
            },
        )

        return {
            "status": "success",
            "message": f"Team '{team_configuration.name}' selected successfully",
            "team_id": selection.team_id,
            "team_name": team_configuration.name,
            "agents_count": len(team_configuration.agents),
            "team_description": team_configuration.description,
        }

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logging.error(f"Error selecting team: {str(e)}")
        track_event_if_configured(
            "Error_Config_Team_Selection",
            {
                "status": "error",
                "team_id": selection.team_id,
                "user_id": user_id,
                "error": str(e),
            },
        )
        raise HTTPException(status_code=500, detail="Internal server error occurred")


# Get plans is called in the initial side rendering of the frontend
@app_v4.get("/plans")
async def get_plans(request: Request):
    """
    Retrieve plans for the current user.

    ---
    tags:
      - Plans
    parameters:
      - name: session_id
        in: query
        type: string
        required: false
        description: Optional session ID to retrieve plans for a specific session
    responses:
      200:
        description: List of plans with steps for the user
        schema:
          type: array
          items:
            type: object
            properties:
              id:
                type: string
                description: Unique ID of the plan
              session_id:
                type: string
                description: Session ID associated with the plan
              initial_goal:
                type: string
                description: The initial goal derived from the user's input
              overall_status:
                type: string
                description: Status of the plan (e.g., in_progress, completed)
              steps:
                type: array
                items:
                  type: object
                  properties:
                    id:
                      type: string
                      description: Unique ID of the step
                    plan_id:
                      type: string
                      description: ID of the plan the step belongs to
                    action:
                      type: string
                      description: The action to be performed
                    agent:
                      type: string
                      description: The agent responsible for the step
                    status:
                      type: string
                      description: Status of the step (e.g., planned, approved, completed)
      400:
        description: Missing or invalid user information
      404:
        description: Plan not found
    """

    user_id, tenant_id = _extract_auth(request)

    # <To do: Francia> Replace the following with code to get plan run history from the database

    # Initialize memory context
    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )

    current_team = await memory_store.get_current_team(user_id=user_id)
    if not current_team:
        return []

    all_plans = await memory_store.get_all_plans_by_team_id_status(
        user_id=user_id, team_id=current_team.team_id, status=PlanStatus.completed
    )

    return all_plans


# Get plans is called in the initial side rendering of the frontend
@app_v4.get("/plan")
async def get_plan_by_id(
    request: Request,
    plan_id: Optional[str] = Query(None),
):
    """
    Retrieve plans for the current user.

    ---
    tags:
      - Plans
    parameters:
      - name: session_id
        in: query
        type: string
        required: false
        description: Optional session ID to retrieve plans for a specific session
    responses:
      200:
        description: List of plans with steps for the user
        schema:
          type: array
          items:
            type: object
            properties:
              id:
                type: string
                description: Unique ID of the plan
              session_id:
                type: string
                description: Session ID associated with the plan
              initial_goal:
                type: string
                description: The initial goal derived from the user's input
              overall_status:
                type: string
                description: Status of the plan (e.g., in_progress, completed)
              steps:
                type: array
                items:
                  type: object
                  properties:
                    id:
                      type: string
                      description: Unique ID of the step
                    plan_id:
                      type: string
                      description: ID of the plan the step belongs to
                    action:
                      type: string
                      description: The action to be performed
                    agent:
                      type: string
                      description: The agent responsible for the step
                    status:
                      type: string
                      description: Status of the step (e.g., planned, approved, completed)
      400:
        description: Missing or invalid user information
      404:
        description: Plan not found
    """

    user_id, tenant_id = _extract_auth(request)

    # <To do: Francia> Replace the following with code to get plan run history from the database

    # Initialize memory context
    memory_store = await DatabaseFactory.get_database(
        user_id=user_id, tenant_id=tenant_id
    )
    try:
        if plan_id:
            plan = await memory_store.get_plan_by_plan_id(plan_id=plan_id)
            if not plan:
                event_props = {"status_code": 400, "detail": "Plan not found"}
                # No session_id available since plan not found
                track_event_if_configured("Error_Plan_Not_Found", event_props)
                raise HTTPException(status_code=404, detail="Plan not found")

            # Attach session_id to span
            if plan.session_id:
                span = trace.get_current_span()
                if span:
                    span.set_attribute("session_id", plan.session_id)

            # Use get_steps_by_plan to match the original implementation

            team = None
            if plan.team_id:
                team = await memory_store.get_team_by_id(team_id=plan.team_id)
            agent_messages: Any = await memory_store.get_agent_messages(
                plan_id=plan.plan_id
            )

            # Merge session chat history (pre-plan conversation) into agent_messages
            if plan.session_id:
                try:
                    chat_svc = await get_chat_cosmos_service()
                    session = await chat_svc.get_session(plan.session_id, user_id)
                    if session and session.get("messages"):
                        session_msgs = []
                        for msg in session["messages"]:
                            role = msg.get("role", "user")
                            metadata = msg.get("metadata") or {}
                            session_msgs.append(
                                {
                                    "agent": "human"
                                    if role == "user"
                                    else metadata.get("agent", "assistant"),
                                    "agent_type": "Human_Agent"
                                    if role == "user"
                                    else "AI_Agent",
                                    "timestamp": msg.get("timestamp"),
                                    "content": msg.get("content", ""),
                                    "steps": [],
                                    "next_steps": [],
                                    "raw_data": msg.get("content", ""),
                                }
                            )
                        # Prepend chat history before plan agent messages
                        agent_messages = session_msgs + list(agent_messages or [])
                except Exception as e:
                    logging.warning(
                        f"Could not load chat history for session {plan.session_id}: {e}"
                    )

            mplan = plan.m_plan if plan.m_plan else None
            streaming_message = plan.streaming_message if plan.streaming_message else ""
            plan.streaming_message = ""  # clear streaming message after retrieval
            plan.m_plan = None  # remove m_plan from plan object for response
            return {
                "plan": plan,
                "team": team if team else None,
                "messages": agent_messages,
                "m_plan": mplan,
                "streaming_message": streaming_message,
            }
        else:
            track_event_if_configured(
                "GetPlanId", {"status_code": 400, "detail": "no plan id"}
            )
            raise HTTPException(status_code=400, detail="no plan id")
    except Exception as e:
        logging.error(f"Error retrieving plan: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error occurred")


# ============================================================================
# MCP Protocol 2025-11-25: UI Resources Endpoints
# ============================================================================


@app_v4.get("/mcp/discovery")
async def discover_mcp_capabilities(
    user_id: str = Query(None), team_id: str = Query(None)
):
    """
    Discovery Init Flow: Get catalog of available MCP UI resources/widgets.

    Provides proactive widget discovery for frontend preload.
    Complements reactive widget rendering (_meta.ui.resourceUri).

    Args:
        user_id: Optional user ID for multi-tenant filtering
        team_id: Optional team ID for connection-based filtering

    Returns:
        Widget catalog with server_id, resource_uri, title, description, etc.
        Example:
        {
            "widgets": [
                {
                    "server_id": "macae-mcp-server",
                    "resource_uri": "ui://product-card/{product_id}",
                    "title": "Product Card Widget",
                    "description": "Interactive product card",
                    "icon": "📦",
                    "tags": ["product", "ecommerce"],
                    "interactive": true,
                    "mimeType": "text/html"
                }
            ],
            "total": 2,
            "cached": false
        }
    """
    try:
        from v4.common.services.mcp_discovery_service import (
            get_mcp_discovery_service,
        )

        discovery_service = get_mcp_discovery_service()

        # Discover widgets for user/team
        widgets = await discovery_service.discover_widgets(
            user_id=user_id, team_id=team_id
        )

        # Build consistent response object
        catalog = {
            "widgets": widgets,
            "total": len(widgets),
            "cached": False,
        }

        track_event_if_configured(
            "MCP_Discovery",
            {"user_id": user_id, "team_id": team_id, "widget_count": catalog["total"]},
        )

        return catalog

    except Exception as e:
        logger.error(f"Error discovering MCP capabilities: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Failed to discover MCP capabilities"
        )


@app_v4.post("/mcp/resources/read")
async def read_mcp_resource(
    body: McpReadResourceRequest,
    user_id: str = Query(None),
):
    """
    Read MCP UI Resource by URI.

    Supports MCP Protocol 2025-11-25 with ui:// scheme for widgets.

    Args:
        body: JSON body with {"uri": "ui://..."}
        user_id: Optional user ID for auth context

    Returns:
        Resource content with mimeType, content, and metadata
    """
    try:
        from v4.common.services.mcp_resource_service import get_mcp_resource_service

        uri = body.uri

        mcp_service = get_mcp_resource_service()
        resource = await mcp_service.read_resource(uri)

        if not resource:
            raise HTTPException(status_code=404, detail=f"Resource not found: {uri}")

        track_event_if_configured(
            "MCP_Resource_Read",
            {"uri": uri, "mimeType": resource.get("mimeType"), "user_id": user_id},
        )

        return resource

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading MCP resource: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to read MCP resource")


@app_v4.get("/mcp/resources/list")
async def list_mcp_resources(user_id: str = Query(None)):
    """
    List all available MCP resources.

    Returns:
        List of resource descriptors
    """
    try:
        from v4.common.services.mcp_resource_service import get_mcp_resource_service

        mcp_service = get_mcp_resource_service()
        resources = await mcp_service.list_resources()

        track_event_if_configured(
            "MCP_Resources_List", {"count": len(resources), "user_id": user_id}
        )

        return {"resources": resources}

    except Exception as e:
        logger.error(f"Error listing MCP resources: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to list MCP resources")


@app_v4.get("/mcp/resources/templates/list")
async def list_mcp_resource_templates(user_id: str = Query(None)):
    """
    List all parameterized resource templates.

    Returns:
        List of resource templates with parameters
    """
    try:
        from v4.common.services.mcp_resource_service import get_mcp_resource_service

        mcp_service = get_mcp_resource_service()
        templates = await mcp_service.list_resource_templates()

        track_event_if_configured(
            "MCP_Resource_Templates_List", {"count": len(templates), "user_id": user_id}
        )

        return {"resourceTemplates": templates}

    except Exception as e:
        logger.error(f"Error listing MCP resource templates: {str(e)}")
        raise HTTPException(
            status_code=500, detail="Failed to list MCP resource templates"
        )


# =========================================================================
# MCP Connections Registry — Server catalog & user connections
# =========================================================================


@app_v4.get("/mcp/connections/servers")
async def list_mcp_servers(request: Request):
    """
    List all available MCP servers in the catalog.

    Returns the shared catalog of MCP servers that agents can connect to.
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        svc = await MCPConnectionsService.get_instance()
        servers = await svc.list_servers(enabled_only=True)

        return {
            "servers": [s.model_dump(mode="json") for s in servers],
            "total": len(servers),
        }
    except Exception as e:
        logger.error(f"Error listing MCP servers: {e}")
        raise HTTPException(status_code=500, detail="Failed to list MCP servers")


@app_v4.post("/mcp/connections/servers")
async def register_mcp_server(entry: MCPServerEntry, request: Request):
    """
    Register a new MCP server in the catalog.

    Body: MCPServerEntry fields (server_name, display_name, endpoint, etc.)
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)
        entry.added_by = get_authenticated_user_details(
            request_headers=request.headers
        ).get("user_name", "unknown")

        svc = await MCPConnectionsService.get_instance()

        # Check for duplicate server_name
        existing = await svc.get_server_by_name(entry.server_name)
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Server '{entry.server_name}' already exists (id={existing.id})",
            )

        result = await svc.upsert_server(entry)

        track_event_if_configured(
            "MCP_Server_Registered",
            {"server_name": result.server_name, "endpoint": result.endpoint},
        )

        return {"server": result.model_dump(mode="json"), "created": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error registering MCP server: {e}")
        raise HTTPException(status_code=500, detail="Failed to register MCP server")


@app_v4.put("/mcp/connections/servers/{server_id}")
async def update_mcp_server(
    server_id: str, body: MCPServerUpdateRequest, request: Request
):
    """
    Update an existing MCP server in the catalog.

    Body: partial MCPServerEntry fields to overwrite (server_name, display_name,
    endpoint, auth_type, oauth_scopes, oauth_authorize_url, oauth_token_url,
    oauth_client_id_env, etc.). The id and server_id are preserved.
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        svc = await MCPConnectionsService.get_instance()

        existing = await svc.get_server(server_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Server not found")

        update_data = body.model_dump(exclude_unset=True)
        existing = existing.model_copy(update=update_data)

        result = await svc.upsert_server(existing)

        track_event_if_configured(
            "MCP_Server_Updated",
            {"server_id": result.id, "server_name": result.server_name},
        )

        return {"server": result.model_dump(mode="json"), "updated": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating MCP server: {e}")
        raise HTTPException(status_code=500, detail="Failed to update MCP server")


@app_v4.delete("/mcp/connections/servers/{server_id}")
async def delete_mcp_server(server_id: str, request: Request):
    """Remove a server from the catalog."""
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        svc = await MCPConnectionsService.get_instance()
        deleted = await svc.delete_server(server_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Server not found")

        track_event_if_configured("MCP_Server_Deleted", {"server_id": server_id})
        return {"deleted": True, "server_id": server_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting MCP server: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete MCP server")


@app_v4.get("/mcp/connections/user")
async def get_user_mcp_connections(request: Request):
    """
    Get all MCP server connections for the authenticated user.

    Returns server catalog merged with user's connection status.
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        svc = await MCPConnectionsService.get_instance()
        result = await svc.get_available_servers_for_user(user_id)

        return {"connections": result, "user_id": user_id}

    except Exception as e:
        logger.error(f"Error getting user connections: {e}")
        raise HTTPException(status_code=500, detail="Failed to get user connections")


@app_v4.get("/mcp/connections/user/{server_name}")
async def get_user_mcp_connection_by_server(server_name: str, request: Request):
    """
    Get a specific user's connection status for a given MCP server.

    Returns the connection object or 404 if no connection exists.
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        svc = await MCPConnectionsService.get_instance()
        conn = await svc.get_user_connection(user_id, server_name)

        if not conn:
            raise HTTPException(
                status_code=404,
                detail=f"No connection found for server '{server_name}'",
            )

        return {"connection": conn.model_dump(mode="json"), "user_id": user_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting user connection: {e}")
        raise HTTPException(status_code=500, detail="Failed to get user connection")


@app_v4.post("/mcp/connections/user/{server_name}/connect")
async def connect_user_to_mcp_server(server_name: str, request: Request):
    """
    Create a user connection entry for an MCP server.

    For servers with auth_type=none, immediately marks as active.
    For servers requiring auth:
    - If credentials provided in body, stores them in Key Vault and marks active
    - Otherwise, marks as pending_auth (OAuth flow)

    Request body (optional):
    {
      "credentials": {
        "access_token": "ghp_xxxx",
        "api_key": "sk_xxxx",
        ...
      }
    }
    """
    try:
        from credential_resolver import CredentialResolver
        from v4.common.models.mcp_connection_models import (
            MCPConnectionStatus,
            MCPUserConnection,
        )
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        svc = await MCPConnectionsService.get_instance()

        # Verify server exists
        server = await svc.get_server_by_name(server_name)
        if not server:
            raise HTTPException(
                status_code=404, detail=f"Server '{server_name}' not found"
            )

        # Check existing connection
        existing = await svc.get_user_connection(user_id, server_name)
        if existing and existing.status == MCPConnectionStatus.ACTIVE:
            return {
                "connection": existing.model_dump(mode="json"),
                "already_connected": True,
            }

        # Parse request body for credentials
        body = {}
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            logger.debug(
                "connect_user_to_mcp_server: request body missing or not valid JSON; proceeding with empty body"
            )

        credentials = body.get("credentials")

        # Determine status and secret_ref
        from v4.common.models.mcp_connection_models import (
            MCPAuthType,
            MCPCredentialSource,
        )

        status = MCPConnectionStatus.PENDING_AUTH
        secret_ref = None
        oauth_url: Optional[str] = None

        if server.auth_type == MCPAuthType.NONE:
            status = MCPConnectionStatus.ACTIVE
        elif server.credential_source == MCPCredentialSource.MANAGED_IDENTITY:
            # Managed Identity tokens are minted by the platform at call time;
            # no user credentials needed — mark active immediately.
            status = MCPConnectionStatus.ACTIVE
        elif credentials:
            try:
                resolver = CredentialResolver()
                secret_ref = await resolver.store_credentials(
                    user_id, server_name, credentials
                )
                status = MCPConnectionStatus.ACTIVE
                logger.info(
                    f"Stored credentials in Key Vault for user '{user_id}' "
                    f"connecting to '{server_name}'"
                )
            except Exception as kv_err:
                logger.error(f"Failed to store credentials in Key Vault: {kv_err}")
                raise HTTPException(
                    status_code=500,
                    detail="Failed to securely store credentials",
                )
        elif server.auth_type == MCPAuthType.OAUTH2:
            from v4.api.oauth_helpers import build_authorize_url, sign_state

            if not server.oauth_authorize_url:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Server '{server_name}' has auth_type=oauth2 but no "
                        f"oauth_authorize_url configured in catalog"
                    ),
                )

            client_id_env = server.oauth_client_id_env or ""
            client_id = os.environ.get(client_id_env, "") if client_id_env else ""
            if not client_id:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"OAuth client_id not configured for '{server_name}' "
                        f"(expected env var: {client_id_env or '<unset>'})"
                    ),
                )

            state = sign_state(user_id, server_name)
            oauth_url = build_authorize_url(
                server.oauth_authorize_url,
                client_id,
                server.oauth_scopes,
                state,
            )

        # Create connection
        conn = MCPUserConnection(
            pk=user_id,
            user_id=user_id,
            server_id=server.id,
            server_name=server_name,
            status=status,
            secret_ref=secret_ref,
        )
        result = await svc.upsert_user_connection(conn)

        track_event_if_configured(
            "MCP_User_Connected",
            {"user_id": user_id, "server_name": server_name, "status": status.value},
        )

        response = {"connection": result.model_dump(mode="json"), "created": True}
        if oauth_url:
            response["oauth_url"] = oauth_url
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error connecting user to MCP server: {e}")
        raise HTTPException(status_code=500, detail="Failed to connect to MCP server")


@app_v4.patch("/mcp/connections/user/{server_name}/activate")
async def activate_user_mcp_connection(server_name: str, request: Request):
    """
    Mark a user's MCP server connection as active.

    Called after OAuth callback completes successfully.
    Body (optional): { "secret_ref": "kv-secret-name" }
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        body = {}
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            logger.debug(
                "activate_user_mcp_connection: request body missing or not valid JSON; proceeding with empty body"
            )

        svc = await MCPConnectionsService.get_instance()
        result = await svc.mark_connection_active(
            user_id, server_name, secret_ref=body.get("secret_ref")
        )

        track_event_if_configured(
            "MCP_User_Activated",
            {"user_id": user_id, "server_name": server_name},
        )

        return {"connection": result.model_dump(mode="json"), "activated": True}

    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error activating MCP connection: {e}")
        raise HTTPException(status_code=500, detail="Failed to activate connection")


@app_v4.get(
    "/mcp/connections/oauth/callback",
    responses={401: {"description": "Invalid or expired state token"}},
)
async def mcp_oauth_callback(query: Annotated[OAuthCallbackQuery, Query()]):
    """OAuth2 redirect callback.

    Verifies the signed state, exchanges the authorization code for a token,
    stores it in Key Vault, marks the user's connection as active, and returns
    an HTML page that closes the popup.
    """
    code, state = query.code, query.state
    from fastapi.responses import HTMLResponse

    from credential_resolver import CredentialResolver
    from v4.api.oauth_helpers import exchange_code_for_token, verify_state
    from v4.common.services.mcp_connections_service import MCPConnectionsService

    def _html(message: str, ok: bool = True, status_code: int = 200) -> HTMLResponse:
        color = "#0a7d3e" if ok else "#b3261e"
        return HTMLResponse(
            content=f"""<!doctype html>
<html><head><meta charset="utf-8"><title>OAuth</title></head>
<body style="font-family:system-ui;padding:32px;text-align:center;">
  <h2 style="color:{color};">{"Conexión exitosa" if ok else "Error en la conexión"}</h2>
  <p style="color:#555;">{message}</p>
  <script>
    try {{ if (window.opener) window.opener.postMessage(
        {{type: 'mcp_oauth', ok: {str(ok).lower()}}}, '*'); }} catch (e) {{}}
    setTimeout(() => window.close(), 1500);
  </script>
</body></html>""",
            status_code=status_code,
        )

    try:
        user_id, server_name = verify_state(state)
    except ValueError as ve:
        logger.warning(f"OAuth callback rejected invalid state: {ve}")
        # 401, not 400: the state is an auth artifact (signed token) and a
        # failed verification is an authorization failure, not a malformed
        # request — the request parsed fine, the credential in it did not.
        return _html(f"Token de estado inválido: {ve}", ok=False, status_code=401)

    svc = await MCPConnectionsService.get_instance()
    server = await svc.get_server_by_name(server_name)
    if not server:
        return _html(
            f"Servidor '{server_name}' no encontrado", ok=False, status_code=404
        )

    client_id_env = server.oauth_client_id_env or ""
    client_secret_env = server.oauth_client_secret_env or ""
    client_id = os.environ.get(client_id_env, "") if client_id_env else ""
    client_secret = os.environ.get(client_secret_env, "") if client_secret_env else ""
    if not client_id or not client_secret or not server.oauth_token_url:
        return _html(
            "OAuth no está completamente configurado en el catálogo.",
            ok=False,
            status_code=500,
        )

    try:
        token_data = await exchange_code_for_token(
            server.oauth_token_url, client_id, client_secret, code
        )
    except Exception as exc:
        logger.error(f"OAuth token exchange failed for '{server_name}': {exc}")
        return _html(
            f"No se pudo intercambiar el código: {exc}", ok=False, status_code=502
        )

    # Enrich the raw provider response with everything credential_resolver needs to
    # REFRESH this token later from ca-mcp — which has neither the token endpoint nor
    # the OAuth client env vars. Without these, oauth_refresh cannot mint a new
    # access_token when the current one expires. Providers rotate refresh_token, so
    # the resolver also writes the rotation back (needs KV Secrets Officer on its MI).
    import time as _time

    token_data = dict(token_data)
    token_data.setdefault("token_endpoint", server.oauth_token_url)
    token_data.setdefault("client_id", client_id)
    if client_secret:
        token_data.setdefault("client_secret", client_secret)
    if server.oauth_scopes:
        token_data.setdefault("scopes", server.oauth_scopes)
    _expires_in = token_data.get("expires_in")
    if _expires_in and "expires_at" not in token_data:
        try:
            token_data["expires_at"] = str(int(_time.time()) + int(_expires_in))
        except (TypeError, ValueError):
            pass

    try:
        resolver = CredentialResolver()
        secret_ref = await resolver.store_credentials(user_id, server_name, token_data)
    except Exception as exc:
        logger.error(f"Failed to store OAuth token in Key Vault: {exc}")
        return _html(
            "No se pudo guardar el token de forma segura.",
            ok=False,
            status_code=500,
        )

    try:
        await svc.mark_connection_active(user_id, server_name, secret_ref=secret_ref)
    except Exception as exc:
        logger.error(f"Failed to mark connection active: {exc}")
        return _html(
            "El token se guardó pero no se pudo activar la conexión.",
            ok=False,
            status_code=500,
        )

    track_event_if_configured(
        "MCP_OAuth_Completed",
        {"user_id": user_id, "server_name": server_name},
    )

    return _html(f"Conectado a {server.display_name}. Puedes cerrar esta ventana.")


@app_v4.delete("/mcp/connections/user/{server_name}")
async def disconnect_user_from_mcp_server(server_name: str, request: Request):
    """Remove a user's connection to an MCP server."""
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        user_id, tenant_id = _extract_auth(request)

        svc = await MCPConnectionsService.get_instance()
        deleted = await svc.disconnect_user(user_id, server_name)

        if not deleted:
            raise HTTPException(status_code=404, detail="Connection not found")

        track_event_if_configured(
            "MCP_User_Disconnected",
            {"user_id": user_id, "server_name": server_name},
        )

        return {"disconnected": True, "server_name": server_name}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error disconnecting from MCP server: {e}")
        raise HTTPException(status_code=500, detail="Failed to disconnect")


@app_v4.get("/mcp/inspector/status")
async def mcp_inspector_status():
    """Status of the MCP Inspector proxy (health + tokenized UI link).

    Existed before the router surgery (May logs show it answering 200) and the
    frontend still calls it (InspectorLink); the service layer survived intact,
    so this is a re-wire to MCPInspectorBridge, not a new subsystem.
    """
    try:
        from v4.common.services.mcp_inspector_bridge import get_inspector_bridge

        return await get_inspector_bridge().get_status()
    except Exception as e:
        logger.error(f"Error getting Inspector status: {e}")
        raise HTTPException(status_code=500, detail="Failed to get Inspector status")
