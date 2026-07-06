import asyncio
import json
import logging
import os
import uuid
from contextlib import AsyncExitStack
from typing import Any, Optional

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

import v4.models.messages as messages
from auth.auth_utils import get_authenticated_user_details
from common.database.database_factory import DatabaseFactory
from common.models.messages_af import (
    ChatMessageRequest,
    ChatMessageResponse,
    InputTask,
    Plan,
    PlanStatus,
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
    responses={404: {"description": "Not found"}},
)


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
    team_switched: bool = Query(False),
):  # add team_switched: bool parameter
    """Initialize the user's current team of agents"""

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
    try:
        memory_store = await DatabaseFactory.get_database(
            user_id=user_id, tenant_id=tenant_id
        )
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

    if not await rai_success(input_task.description, team, memory_store):
        track_event_if_configured(
            "Error_RAI_Check_Failed",
            {
                "status": "Plan not created - RAI check failed",
                "description": input_task.description,
                "session_id": input_task.session_id,
            },
        )
        raise HTTPException(
            status_code=400,
            detail="Request contains content that doesn't meet our safety guidelines, try again.",
        )

    if not input_task.session_id:
        input_task.session_id = str(uuid.uuid4())

    # Attach session_id to current span for Application Insights
    span = trace.get_current_span()
    if span:
        span.set_attribute("session_id", input_task.session_id)

    try:
        plan_id = str(uuid.uuid4())
        # Initialize memory store and service
        plan = Plan(
            id=plan_id,
            plan_id=plan_id,
            user_id=user_id,
            session_id=input_task.session_id,
            team_id=team_id,
            initial_goal=input_task.description,
            overall_status=PlanStatus.in_progress,
        )
        await memory_store.add_plan(plan)

        try:
            _chat_svc = await get_chat_cosmos_service()
            await _chat_svc.add_message(
                session_id=input_task.session_id,
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
                "session_id": input_task.session_id,
                "user_id": user_id,
                "team_id": team_id,
                "description": input_task.description,
            },
        )
    except Exception as e:
        print(f"Error creating plan: {e}")
        track_event_if_configured(
            "Error_Plan_Creation_Failed",
            {
                "status": "error",
                "description": input_task.description,
                "session_id": input_task.session_id,
                "user_id": user_id,
                "error": str(e),
            },
        )
        raise HTTPException(status_code=500, detail="Failed to create plan") from e

    try:

        async def run_orchestration_task():
            try:
                await OrchestrationManager().run_orchestration(
                    user_id, input_task.session_id, input_task
                )
            finally:
                orchestration_config.clear_run_active(input_task.session_id)

        # Mark the session's run in flight BEFORE returning, so a near-immediate
        # resume_plan from PlanPage (orphan recovery on a freshly-created plan)
        # is skipped instead of starting a duplicate orchestration.
        orchestration_config.mark_run_active(input_task.session_id)
        background_tasks.add_task(run_orchestration_task)

        return {
            "status": "Request started successfully",
            "session_id": input_task.session_id,
            "plan_id": plan_id,
        }

    except Exception as e:
        track_event_if_configured(
            "Error_Request_Start_Failed",
            {
                "session_id": input_task.session_id,
                "description": input_task.description,
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
    creds = app_config.get_azure_credential_async(app_config.AZURE_CLIENT_ID)

    contents = await file.read()
    filename = file.filename or "upload"

    if not contents:
        raise HTTPException(
            status_code=400,
            detail="The uploaded file is empty. Please select a file with content.",
        )

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


@app_v4.get("/chat/download-file/{file_id}")
async def chat_download_file(
    file_id: str,
    container_id: str | None = None,
    request: Request = None,  # type: ignore[assignment]
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

    try:
        if file_id.startswith("cfile_"):
            if not container_id:
                raise HTTPException(
                    status_code=400,
                    detail="container_id is required to download generated container files",
                )

            project = app_config.get_ai_project_client()
            try:
                openai = project.get_openai_client()
                try:
                    file_info = await openai.containers.files.retrieve(
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
                finally:
                    await openai.close()
            finally:
                if hasattr(project, "close"):
                    await project.close()
        else:
            from azure.ai.agents.aio import AgentsClient

            creds = app_config.get_azure_credential_async(app_config.AZURE_CLIENT_ID)
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

        # Infer a reasonable content-type from the filename extension
        import mimetypes

        mime, _ = mimetypes.guess_type(filename)
        mime = mime or "application/octet-stream"

        from fastapi.responses import Response

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
            },
        )
    except HTTPException:
        raise  # preserve the original status code (e.g. 400 for missing container_id)
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

    # When the message originates from an open plan, never create a new plan.
    if chat_request.plan_id and intent_result.intent == Intent.TASK:
        logger.info(
            "plan_id=%s present — downgrading TASK to CONVERSATIONAL",
            chat_request.plan_id,
        )
        from v4.orchestration.intent_router import IntentResult

        intent_result = IntentResult(
            intent=Intent.CONVERSATIONAL,
            confidence=intent_result.confidence,
            reasoning="plan_id present — in-plan follow-up, not a new task",
        )

    # ── Route by intent ──────────────────────────────────────────
    if intent_result.intent == Intent.TASK:
        input_task_for_plan = InputTask(
            session_id=chat_request.session_id,
            description=chat_request.message,
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
                deployment_name="",
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

    workflow = await OrchestrationManager.init_direct_response_orchestration(
        agents=agents,
        team_config=direct_team,
        user_id=user_id,
    )
    return workflow, agents, direct_team


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


@app_v4.post("/chat/message/stream")
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

    from v4.orchestration.intent_router import Intent, IntentRouter

    authenticated_user = get_authenticated_user_details(request_headers=request.headers)
    user_id = authenticated_user["user_principal_id"]
    tenant_id = authenticated_user.get("tenant_id", "")
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

    if active_plan:
        from v4.orchestration.intent_router import IntentResult

        intent_result = IntentResult(
            intent=Intent.CONVERSATIONAL,
            confidence=1.0,
            reasoning="active plan follow-up",
        )
    else:
        intent_result = await IntentRouter.classify_async(
            chat_request.message, previous_intent=previous_intent
        )
    logger.info(
        "Chat stream intent: %s (confidence=%.2f, prev=%s) for message: %s",
        intent_result.intent.value,
        intent_result.confidence,
        previous_intent,
        chat_request.message[:80],
    )

    if active_plan and intent_result.intent == Intent.TASK:
        logger.info(
            "plan_id=%s present — routing TASK as active plan follow-up",
            chat_request.plan_id,
        )
        from v4.orchestration.intent_router import IntentResult

        intent_result = IntentResult(
            intent=Intent.CONVERSATIONAL,
            confidence=intent_result.confidence,
            reasoning="active plan follow-up, not a new task",
        )

    plan_id: Optional[str] = None
    if intent_result.intent == Intent.TASK:
        try:
            input_task = InputTask(
                session_id=chat_request.session_id,
                description=chat_request.message,
            )
            result = await process_request(background_tasks, input_task, request)
            plan_id = result.get("plan_id")
        except Exception as e:
            logger.error("Error creating plan from streaming chat: %s", e)
            plan_id = None

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
            from v4.magentic_agents.foundry_agent import FoundryAgentTemplate

            # Create merged team with ALL agents from ALL teams
            (
                workflow,
                direct_agents,
                direct_team,
            ) = await _create_direct_response_workflow(
                user_id=user_id,
                tenant_id=tenant_id,
                team_config_input=active_plan_team,
                user_access_token=user_access_token,
                proxy_only=True,
            )
            direct_team_name = getattr(direct_team, "name", "Direct Response Team")
            for _a in direct_agents:
                if callable(getattr(_a, "close", None)):
                    _cleanup.push_async_callback(_a.close)

            # Select best agent from merged team using keyword scoring
            foundry_agents = [
                a for a in direct_agents if isinstance(a, FoundryAgentTemplate)
            ]
            if not foundry_agents:
                raise ValueError("No FoundryAgent available in merged team")

            agent = await _select_team_agent(
                chat_request.message, direct_team, foundry_agents
            )
            selected_agent_name = _agent_runtime_name(agent) or "assistant"
            logger.info(
                "Selected '%s' from merged team (session=%s, %d agents available)",
                selected_agent_name,
                chat_request.session_id[:12],
                len(foundry_agents),
            )

            direct_chat_prompt = (
                _build_plan_chat_prompt(
                    chat_request.message,
                    direct_team,
                    agent,
                    active_plan,
                )
                if active_plan
                else _build_direct_chat_prompt(
                    chat_request.message,
                    direct_team,
                    agent,
                )
            )

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
                        mcp_cfg.name, tenant_id=tenant_id
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

            # Recover Foundry conversation thread across re-auth / process restarts.
            # The conversation_id is persisted in Cosmos after each turn so the
            # next invoke can resume the same Foundry thread instead of starting fresh.
            _foundry_conv_id: Optional[str] = None
            try:
                _foundry_conv_id = await chat_svc.get_foundry_conversation_id(
                    chat_request.session_id, user_id
                )
                if _foundry_conv_id:
                    logger.info(
                        "Resuming Foundry conversation thread: conv_id=%s session=%s",
                        _foundry_conv_id,
                        chat_request.session_id[:12],
                    )
            except Exception as _conv_err:
                logger.warning("Could not load foundry_conversation_id: %s", _conv_err)

            _invoke_kwargs: dict = {
                "session_id": chat_request.session_id,
                "user_id": user_id,
                "file_ids": chat_request.file_ids,
            }
            if _foundry_conv_id:
                _invoke_kwargs["previous_response_id"] = _foundry_conv_id

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
                        logger.info(
                            "Function result: name=%s result=%s",
                            getattr(content, "name", "?"),
                            str(getattr(content, "result", content))[:4000],
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
                                }
                            )

                    elif ct == "mcp_server_tool_call":
                        tool_name = getattr(content, "tool_name", None) or "unknown"
                        server_name = getattr(content, "server_name", None) or "unknown"
                        last_mcp_tool_call = (tool_name, server_name)
                        _key = ("calling", tool_name, server_name)
                        if _key != _last_tool_activity_key:
                            _last_tool_activity_key = _key
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
                        _key = ("result", tool_name, server_name)
                        if _key != _last_tool_activity_key:
                            _last_tool_activity_key = _key
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
            await chat_svc.add_message(
                session_id=chat_request.session_id,
                user_id=user_id,
                content=full_text,
                role="assistant",
                metadata=_persist_meta,
            )
        except Exception as e:
            logger.warning("Could not persist streamed response: %s", e)

        # Persist the Foundry conversation_id so the next turn resumes the same thread.
        # Extract it from the agent's last response_id if available.
        try:
            _new_conv_id = getattr(agent, "_last_response_id", None) or getattr(
                agent, "last_response_id", None
            )
            if not _new_conv_id and hasattr(agent, "_agent"):
                _new_conv_id = getattr(
                    agent._agent, "_last_response_id", None
                ) or getattr(agent._agent, "last_response_id", None)
            if _new_conv_id and _new_conv_id != _foundry_conv_id:
                await chat_svc.set_foundry_conversation_id(
                    chat_request.session_id, user_id, _new_conv_id
                )
                logger.info(
                    "Persisted new Foundry conversation_id=%s for session=%s",
                    _new_conv_id,
                    chat_request.session_id[:12],
                )
        except Exception as _persist_conv_err:
            logger.warning(
                "Could not persist foundry_conversation_id: %s", _persist_conv_err
            )

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
    await chat_svc.clear_foundry_conversation_id(session_id, user_id)  # type: ignore
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
    payload: dict,
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
    plan_id = payload.get("plan_id", "")
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
                user_id, plan.session_id, input_task
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
            agent_messages = await memory_store.get_agent_messages(plan_id=plan.plan_id)

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
                                    "agent_type": "human" if role == "user" else "ai",
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
async def read_mcp_resource(request: Request, user_id: str = Query(None)):
    """
    Read MCP UI Resource by URI.

    Supports MCP Protocol 2025-11-25 with ui:// scheme for widgets.

    Args:
        request: FastAPI request with JSON body {"uri": "ui://..."}
        user_id: Optional user ID for auth context

    Returns:
        Resource content with mimeType, content, and metadata
    """
    try:
        from v4.common.services.mcp_resource_service import get_mcp_resource_service

        # Parse request body
        body = await request.json()
        uri = body.get("uri")

        if not uri:
            raise HTTPException(status_code=400, detail="Missing 'uri' in request body")

        # Get MCP resource service
        mcp_service = get_mcp_resource_service()

        # Read resource from MCP server
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
async def register_mcp_server(request: Request):
    """
    Register a new MCP server in the catalog.

    Body: MCPServerEntry fields (server_name, display_name, endpoint, etc.)
    """
    try:
        from v4.common.models.mcp_connection_models import MCPServerEntry
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        body = await request.json()
        entry = MCPServerEntry(**body)

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
async def update_mcp_server(server_id: str, request: Request):
    """
    Update an existing MCP server in the catalog.

    Body: partial MCPServerEntry fields to overwrite (server_name, display_name,
    endpoint, auth_type, oauth_scopes, oauth_authorize_url, oauth_token_url,
    oauth_client_id_env, etc.). The id and server_id are preserved.
    """
    try:
        from v4.common.services.mcp_connections_service import MCPConnectionsService

        body = await request.json()

        user_id, tenant_id = _extract_auth(request)

        svc = await MCPConnectionsService.get_instance()

        existing = await svc.get_server(server_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Server not found")

        # Apply provided fields onto the existing entry. Never allow the body to
        # change the document identity / partition metadata.
        protected = {"id", "pk", "doc_type", "created_at"}
        for key, value in body.items():
            if key in protected:
                continue
            if hasattr(existing, key):
                setattr(existing, key, value)

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
        except Exception:
            pass

        credentials = body.get("credentials")

        # Determine status and secret_ref
        from v4.common.models.mcp_connection_models import MCPAuthType

        status = MCPConnectionStatus.PENDING_AUTH
        secret_ref = None
        oauth_url: Optional[str] = None

        if server.auth_type == MCPAuthType.NONE:
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
        except Exception:
            pass

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


@app_v4.get("/mcp/connections/oauth/callback")
async def mcp_oauth_callback(code: str, state: str):
    """OAuth2 redirect callback.

    Verifies the signed state, exchanges the authorization code for a token,
    stores it in Key Vault, marks the user's connection as active, and returns
    an HTML page that closes the popup.
    """
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
        return _html(f"Token de estado inválido: {ve}", ok=False, status_code=400)

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
