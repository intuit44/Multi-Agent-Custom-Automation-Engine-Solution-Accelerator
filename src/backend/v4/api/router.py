import asyncio
import json
import logging
import os
import uuid
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
        user_id, tenant_id = _extract_auth(request)

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
    user_id, tenant_id = _extract_auth(request)
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
                content="I've created a plan for your request. Redirecting to plan view.",
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
            await OrchestrationManager().run_orchestration(
                user_id, input_task.session_id, input_task
            )

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
    file: UploadFile = File(...),
    request: Request = None,  # type: ignore
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
    agent_response = await _get_mcp_query_response(
        chat_request.message,
        chat_request.session_id,
        user_id,
        chat_svc,
        tenant_id=tenant_id,
        user_access_token=user_access_token,
    )

    # ── Clasificar con la respuesta del agente como contexto ────────────
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

    # ── Route by intent ──────────────────────────────────────────
    if intent_result.intent == Intent.TASK:
        # La descripción del plan incluye la respuesta del agente para que
        # los sub-agentes arranquen con el contexto completo de la KB.
        input_task_for_plan = InputTask(
            session_id=chat_request.session_id,
            description=(
                f"{chat_request.message}\n\nContext from knowledge base:\n{agent_response}"
                if agent_response
                else chat_request.message
            ),
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
        # Respuesta ya obtenida — devolverla directamente.
        actual_intent = intent_result.intent.value  # "mcp_query" or "conversational"
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
    authenticated_user.get("access_token")  # For OBO flow

    if not chat_request.session_id:
        chat_request.session_id = str(uuid.uuid4())

    # ── Pre-stream work: persist user message + classify intent ──
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

    if previous_intent == "task":
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

    # ── Manager intercepta según intent ──
    # El Manager decide el flujo DESPUÉS de clasificación, ANTES de crear plan
    requires_plan_creation = False

    if intent_result.intent == Intent.TASK:
        # Manager decide si esto requiere plan o solo respuesta
        # Por ahora: TODO intent TASK requiere plan
        # Futuro: Manager podría decir "Esto es simple, no necesita plan"
        requires_plan_creation = True

    # ── SSE async generator ──────────────────────────────────────
    async def event_stream():
        # 1. Intent event
        yield _sse_event(
            {
                "type": "intent",
                "intent": intent_result.intent.value,
                "confidence": intent_result.confidence,
                "session_id": chat_request.session_id,
            }
        )

        # 2. Handle task intent → Manager says "I'll create a plan..." BEFORE creating
        if intent_result.intent == Intent.TASK and requires_plan_creation:
            # MANAGER: Announce plan creation BEFORE executing
            manager_announce = "I'll create a plan for your request..."
            yield _sse_event({"type": "token", "content": manager_announce})

            # THEN: Create the plan
            plan_id_created: Optional[str] = None
            try:
                input_task = InputTask(
                    session_id=chat_request.session_id,
                    description=chat_request.message,
                )
                result = await process_request(background_tasks, input_task, request)
                plan_id_created = result.get("plan_id")
            except Exception as e:
                logger.error("Error creating plan from streaming chat: %s", e)
                plan_id_created = None

            # MANAGER: Confirm plan creation or error
            if plan_id_created:
                confirm_msg = "\n✅ Plan created. Redirecting to plan view."
                yield _sse_event({"type": "token", "content": confirm_msg})
                yield _sse_event(
                    {
                        "type": "plan_created",
                        "plan_id": plan_id_created,
                        "session_id": chat_request.session_id,
                    }
                )
            else:
                error_msg = "\n❌ Sorry, I couldn't create a plan. Please try again."
                yield _sse_event(
                    {
                        "type": "token",
                        "content": error_msg,
                    }
                )

            yield _sse_event(
                {
                    "type": "done",
                    "intent": "task",
                    "agent": "manager",
                    "confidence": intent_result.confidence,
                    "session_id": chat_request.session_id,
                }
            )
            return

        # 3. Unified FoundryAgent for ALL non-task intents
        #    One brain: knowledge_base_retrieve + MCP tools + reasoning.
        #    Emits rich SSE events so the UI can show intermediate steps.
        full_text = ""
        collected_generated_files: list[dict] = []

        try:
            code_interpreter_call_emitted = False
            from v4.config.agent_pool import get_or_create
            from v4.magentic_agents.foundry_agent import FoundryAgentTemplate
            from v4.magentic_agents.magentic_agent_factory import MagenticAgentFactory

            _memory_store = await DatabaseFactory.get_database(user_id=user_id)
            _user_team = await _memory_store.get_current_team(user_id=user_id)
            if not _user_team:
                raise ValueError("No active team configured for user")
            _team = await _memory_store.get_team_by_id(team_id=_user_team.team_id)

            async def _factory():
                if not _team:
                    raise ValueError("No active team configured for user")

                # FIRST: Try to reuse FoundryAgent from existing orchestration
                # Do NOT create new agents for conversational chat.
                # The orchestration was initialized in /init_team.
                existing_orch = orchestration_config.get_current_orchestration(user_id)
                if existing_orch:
                    # Extract agent wrappers (FoundryAgentTemplate instances)
                    agents = orchestration_config.agent_wrappers.get(user_id, [])
                    foundry_agents = [
                        a for a in agents if isinstance(a, FoundryAgentTemplate)
                    ]
                    # Prefer agent with RAG enabled
                    reused_agent = next(
                        (a for a in foundry_agents if getattr(a, "use_rag", False)),
                        foundry_agents[0] if foundry_agents else None,
                    )
                    if reused_agent:
                        logger.info(
                            "Reusing FoundryAgent '%s' from existing orchestration for session=%s",
                            getattr(reused_agent, "agent_name", "?"),
                            chat_request.session_id[:12],
                        )
                        return reused_agent

                # FALLBACK: Create new agent only if orchestration doesn't exist
                logger.warning(
                    "No existing orchestration for user=%s; creating agent for session=%s",
                    user_id[:8],
                    chat_request.session_id[:12],
                )
                _factory_store = await DatabaseFactory.get_database(user_id=user_id)
                _all_agents = await MagenticAgentFactory().get_agents(
                    user_id, _team, _factory_store
                )
                logger.info("DEBUG: _all_agents returned %d agents", len(_all_agents))
                for i, a in enumerate(_all_agents):
                    logger.info(
                        "  Agent %d: type=%s, name=%s, has_invoke=%s",
                        i,
                        type(a).__name__,
                        getattr(a, "agent_name", getattr(a, "name", "?")),
                        hasattr(a, "invoke"),
                    )
                # Only FoundryAgentTemplate has .invoke() — ProxyAgent does not
                _foundry = [
                    a for a in _all_agents if isinstance(a, FoundryAgentTemplate)
                ]
                logger.info(
                    "DEBUG: After filtering FoundryAgentTemplate: %d agents",
                    len(_foundry),
                )
                for i, a in enumerate(_foundry):
                    logger.info(
                        "  Filtered Agent %d: name=%s, use_rag=%s",
                        i,
                        getattr(a, "agent_name", "?"),
                        getattr(a, "use_rag", False),
                    )
                # Prefer use_rag=True; fall back to any FoundryAgentTemplate
                _agent = next(
                    (a for a in _foundry if getattr(a, "use_rag", False)),
                    _foundry[0] if _foundry else None,
                )
                logger.info(
                    "DEBUG: Selected agent: %s (type=%s, use_rag=%s)",
                    getattr(_agent, "agent_name", "?"),
                    type(_agent).__name__ if _agent else "None",
                    getattr(_agent, "use_rag", False) if _agent else "N/A",
                )
                if not _agent:
                    raise ValueError(
                        "Team has no FoundryAgentTemplate agents with invoke()"
                    )
                return _agent

            agent = await get_or_create(
                tenant_id, user_id, chat_request.session_id, _factory
            )

            async for update in agent.invoke(
                chat_request.message,
                session_id=chat_request.session_id,
                user_id=user_id,
                file_ids=chat_request.file_ids,
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
                        "SSE content type=%s, name=%s, text=%s",
                        ct,
                        getattr(content, "name", None)
                        or getattr(content, "tool_name", None)
                        or "",
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
                        yield _sse_event(
                            {
                                "type": "tool_activity",
                                "activity": "result",
                                "tool": content.name or "unknown",
                                "success": content.exception is None,
                            }
                        )

                    elif ct == "mcp_server_tool_call":
                        yield _sse_event(
                            {
                                "type": "tool_activity",
                                "activity": "calling",
                                "tool": content.tool_name or "unknown",
                                "server": content.server_name or "unknown",
                                "args": str(content.arguments or "")[:200],
                            }
                        )

                    elif ct == "mcp_server_tool_result":
                        yield _sse_event(
                            {
                                "type": "tool_activity",
                                "activity": "result",
                                "tool": content.tool_name or "unknown",
                                "server": content.server_name or "unknown",
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
                        # Emit generated_file events for any file produced by code_interpreter.
                        # agent_framework's Annotation TypedDict shape (verified in source):
                        #   ann["file_id"]                            → file_id
                        #   ann["url"]                                → filename (yes, it's stored in 'url')
                        #   ann["additional_properties"]["container_id"] → container_id
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
            logger.warning("FoundryAgent streaming failed (%s), using fallback", e)
            if not full_text:
                full_text = _mcp_fallback(chat_request.message)
                yield _sse_event({"type": "token", "content": full_text})
            else:
                # Partial text was already sent — notify frontend of the interruption
                # so the user sees an error instead of silent truncation.
                yield _sse_event(
                    {
                        "type": "error",
                        "message": "Stream interrupted unexpectedly. Please try again.",
                    }
                )

        # 4. Persist full assistant response to Cosmos
        try:
            _persist_meta: dict = {"intent": intent_result.intent.value}
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

        # MANAGER: Final consolidation message
        if intent_result.intent.value == "mcp_query":
            consolidation_msg = "\n✅ Query completed. Here's what I found."
        else:  # conversational
            consolidation_msg = "\n✅ Response ready."

        yield _sse_event({"type": "token", "content": consolidation_msg})

        track_event_if_configured(
            "Chat_Streaming",
            {
                "session_id": chat_request.session_id,
                "user_id": user_id,
                "intent": intent_result.intent.value,
                "response_length": len(full_text),
            },
        )

        # 5. Done event with final metadata
        yield _sse_event(
            {
                "type": "done",
                "intent": intent_result.intent.value,
                "agent": "manager",
                "confidence": intent_result.confidence,
                "session_id": chat_request.session_id,
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


# ── MCP Agent (FoundryAgentTemplate with tool execution) ────────

_MCP_AGENT_INSTRUCTIONS = (
    "You are the MACAE assistant.\n"
    "For multi-step work, begin with a concise checklist of what you will do.\n"
    "You do not have persistent conversational memory unless a tool returns it.\n"
    "Use tools to retrieve real context instead of claiming memory.\n\n"
    "═══ 1. KNOWLEDGE BASE (knowledge_base_retrieve) ═══\n"
    "You have a tool called knowledge_base_retrieve that searches across ALL knowledge bases:\n"
    "chat history, contracts, RFPs, customer data, order data, and compliance documents.\n\n"
    "MEMORY RULE — MANDATORY:\n"
    "You MUST call knowledge_base_retrieve BEFORE saying you don't have memory or context.\n"
    "When the user asks about:\n"
    "  - Previous sessions, conversations, interactions, or history\n"
    "  - Past errors, problems, or issues they had\n"
    "  - Any domain knowledge (contracts, NDAs, policies, products, customers)\n"
    "→ ALWAYS call knowledge_base_retrieve(query='<relevant search terms>') FIRST.\n"
    "→ NEVER say 'I don't have memory' without searching first.\n\n"
    "NO-FABRICATION RULE — STRICT:\n"
    "After calling knowledge_base_retrieve, examine the actual returned content.\n"
    "  - If results contain documents: cite them by REAL source_blob (only what the\n"
    "    tool returned, never invent paths like 'blob://...' or made-up filenames).\n"
    "  - If results are empty or whitespace-only: respond literally: 'I searched the\n"
    "    knowledge base for [query] and found 0 documents.' Then STOP.\n"
    "  - NEVER fabricate document titles, source_blob paths, or content.\n"
    "  - NEVER claim 'found N documents' if the tool returned empty.\n"
    "  - NEVER invent paths in formats like 'blob://kbXX/...' — those are NOT real\n"
    "    source_blobs. Real source_blobs are filenames (e.g. 'NDA_file.docx') or\n"
    "    full SharePoint/Blob URLs.\n\n"
    "Parameters:\n"
    "  - query: Natural language search (e.g. 'NDA Contoso risks', 'customer churn Q1')\n"
    "  - source_type: 'all' (default), 'chat' (history only), 'documents' (docs only)\n\n"
    "═══ 2. CODE INTERPRETER ═══\n"
    "code_interpreter is available ONLY when the user explicitly uploads a file in the chat.\n"
    "When files are attached to this message, they are available in the sandbox.\n\n"
    "RULES:\n"
    "- Use code_interpreter ONLY when the user has uploaded a file AND asks you to analyze it.\n"
    "- NEVER call code_interpreter to explore the filesystem, list directories, or find files.\n"
    "- NEVER call code_interpreter speculatively (without a user-uploaded file attached).\n"
    "- Do NOT use code_interpreter to answer questions about domain data — use knowledge_base_retrieve instead.\n\n"
    "CORRECT flow when a file is attached:\n"
    "  1. User uploads a CSV/Excel/JSON and asks a question about it.\n"
    "  2. Call code_interpreter to read and analyze the attached file content.\n"
    "  3. Return the result directly.\n\n"
    "STRICT PROHIBITIONS:\n"
    "- Do NOT call list_connected_servers, connect_mcp_server, connect_stdio_server,\n"
    "  connect_from_registry, discover_mcp_capabilities, call_external_tool,\n"
    "  read_external_resource, or disconnect_mcp_server.\n\n"
    "Be concise, report real results, respond in the user's language."
)


async def _get_mcp_query_response(
    message: str,
    session_id: str,
    user_id: str,
    chat_svc: Any,
    tenant_id: str = "",
    user_access_token: Optional[str] = None,
) -> str:
    """Get response using server-side ChatMCPAgent (published in Foundry with KB).

    Args:
        user_access_token: User's EasyAuth access token for OBO flow in Foundry.
    """
    from v4.config.agent_pool import get_or_create
    from v4.magentic_agents.magentic_agent_factory import MagenticAgentFactory

    try:
        _memory_store = await DatabaseFactory.get_database(user_id=user_id)
        _user_team = await _memory_store.get_current_team(user_id=user_id)
        if not _user_team:
            raise ValueError("No active team configured for user")
        _team = await _memory_store.get_team_by_id(team_id=_user_team.team_id)

        async def _factory():
            if not _team:
                raise ValueError("No active team configured for user")
            from v4.magentic_agents.foundry_agent import FoundryAgentTemplate

            # FIRST: Try to reuse FoundryAgent from existing orchestration
            existing_orch = orchestration_config.get_current_orchestration(user_id)
            if existing_orch:
                agents = orchestration_config.agent_wrappers.get(user_id, [])
                foundry_agents = [
                    a for a in agents if isinstance(a, FoundryAgentTemplate)
                ]
                reused_agent = next(
                    (a for a in foundry_agents if getattr(a, "use_rag", False)),
                    foundry_agents[0] if foundry_agents else None,
                )
                if reused_agent:
                    logger.info(
                        "Reusing FoundryAgent from existing orchestration for session=%s",
                        session_id[:12],
                    )
                    return reused_agent

            # FALLBACK: Create new agent if no orchestration exists
            logger.warning(
                "No existing orchestration for user=%s; creating agent for session=%s",
                user_id[:8],
                session_id[:12],
            )
            _factory_store = await DatabaseFactory.get_database(user_id=user_id)
            _all_agents = await MagenticAgentFactory().get_agents(
                user_id, _team, _factory_store
            )
            logger.info("DEBUG: _all_agents returned %d agents", len(_all_agents))
            for i, a in enumerate(_all_agents):
                logger.info(
                    "  Agent %d: type=%s, name=%s, has_invoke=%s",
                    i,
                    type(a).__name__,
                    getattr(a, "agent_name", getattr(a, "name", "?")),
                    hasattr(a, "invoke"),
                )
            # Only FoundryAgentTemplate has .invoke() — ProxyAgent does not
            _foundry = [a for a in _all_agents if isinstance(a, FoundryAgentTemplate)]
            logger.info(
                "DEBUG: After filtering FoundryAgentTemplate: %d agents", len(_foundry)
            )
            for i, a in enumerate(_foundry):
                logger.info(
                    "  Filtered Agent %d: name=%s, use_rag=%s",
                    i,
                    getattr(a, "agent_name", "?"),
                    getattr(a, "use_rag", False),
                )
            # Prefer use_rag=True; fall back to any FoundryAgentTemplate
            _agent = next(
                (a for a in _foundry if getattr(a, "use_rag", False)),
                _foundry[0] if _foundry else None,
            )
            logger.info(
                "DEBUG: Selected agent: %s (type=%s, use_rag=%s)",
                getattr(_agent, "agent_name", "?"),
                type(_agent).__name__ if _agent else "None",
                getattr(_agent, "use_rag", False) if _agent else "N/A",
            )
            if not _agent:
                raise ValueError(
                    "Team has no FoundryAgentTemplate agents with invoke()"
                )
            return _agent

        agent = await get_or_create(tenant_id, user_id, session_id, _factory)

        full_text = ""
        async for update in agent.invoke(
            message, session_id=session_id, user_id=user_id
        ):
            token = getattr(update, "text", "") or ""
            if token:
                full_text += token
        return full_text if full_text else _mcp_fallback(message)

    except Exception as e:
        logger.warning("MCP agent query failed (%s), using fallback", e)
        return _mcp_fallback(message)


def _mcp_fallback(message: str) -> str:
    """Fallback response if MCP agent fails."""
    return (
        "Sorry, I'm having trouble connecting to my tools right now. "
        "Please try again later."
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


@app_v4.delete("/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str, request: Request):
    """Delete a chat session."""
    user_id, tenant_id = _extract_auth(request)

    chat_svc = await get_chat_cosmos_service()
    deleted = await chat_svc.delete_session(session_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, "message": "Session deleted"}


@app_v4.post("/resume_plan")
async def resume_plan(
    background_tasks: BackgroundTasks,
    payload: dict,
    request: Request,
):
    """
    Resume orchestration for an existing in_progress plan whose m_plan is null.
    Used by the frontend when it detects an orphaned plan on page load.
    """
    user_id, tenant_id = _extract_auth(request)
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
    )

    input_task = InputTask(description=plan.initial_goal, session_id=plan.session_id)

    async def run_orchestration_task():
        await OrchestrationManager().run_orchestration(
            user_id, plan.session_id, input_task
        )

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
    For servers requiring auth, marks as pending_auth.
    """
    try:
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

        # Create connection
        from v4.common.models.mcp_connection_models import MCPAuthType

        status = (
            MCPConnectionStatus.ACTIVE
            if server.auth_type == MCPAuthType.NONE
            else MCPConnectionStatus.PENDING_AUTH
        )

        conn = MCPUserConnection(
            pk=user_id,
            user_id=user_id,
            server_id=server.id,
            server_name=server_name,
            status=status,
        )
        result = await svc.upsert_user_connection(conn)

        track_event_if_configured(
            "MCP_User_Connected",
            {"user_id": user_id, "server_name": server_name, "status": status.value},
        )

        return {"connection": result.model_dump(mode="json"), "created": True}

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


# =========================================================================
# MCP Inspector: Status & Management Endpoints
# =========================================================================


@app_v4.get("/mcp/inspector/status")
async def get_inspector_status():
    """
    Get MCP Inspector proxy status and UI link.

    Returns:
        Inspector running state, proxy URL, UI URL, and pre-filled link.
    """
    try:
        from v4.common.services.mcp_inspector_bridge import get_inspector_bridge

        bridge = get_inspector_bridge()
        status = await bridge.get_status()

        track_event_if_configured(
            "MCP_Inspector_Status",
            {"running": status.get("running", False)},
        )

        return status

    except Exception as e:
        logger.error(f"Error checking Inspector status: {str(e)}")
        return {
            "running": False,
            "ui_link": (
                "http://localhost:6274/"
                "?transport=streamable-http"
                "&serverUrl=http://localhost:9000/mcp"
            ),
            "error": "Failed to check Inspector status",
        }
