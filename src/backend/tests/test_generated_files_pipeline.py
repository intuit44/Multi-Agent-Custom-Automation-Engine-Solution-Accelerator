"""
Integration tests for the generated-files pipeline.

Strategy
--------
Mock ONLY the external I/O boundaries:
  - Azure AI agent (get_or_create / FoundryAgentTemplate.invoke)
  - Cosmos DB  (ChatCosmosService.add_message / get_session)
  - Azure AI Project client (download endpoint)
  - IntentRouter (to force "conversational" so we stay in the streaming branch)

The REAL router code runs end-to-end:
  - event_stream() generator
  - SSE event formatting
  - collected_generated_files accumulation
  - _persist_meta construction
  - download-file routing (cfile_ vs regular)

Markers
-------
  @pytest.mark.unit   — fast, no network, always runs
"""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Environment defaults so app_config imports don't fail at collection time
# ---------------------------------------------------------------------------
for _k, _v in {
    "COSMOSDB_ENDPOINT": "https://mock-cosmos.documents.azure.com:443/",
    "COSMOSDB_KEY": "mock-key==",
    "COSMOSDB_DATABASE": "mock-db",
    "COSMOSDB_CONTAINER": "mock-container",
    "AZURE_OPENAI_DEPLOYMENT_NAME": "gpt-4o-mock",
    "AZURE_OPENAI_API_VERSION": "2024-05-01-preview",
    "AZURE_OPENAI_ENDPOINT": "https://mock-openai.openai.azure.com/",
    "AZURE_AI_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
    "AZURE_AI_RESOURCE_GROUP": "rg-mock",
    "AZURE_AI_PROJECT_NAME": "proj-mock",
    "AZURE_AI_AGENT_ENDPOINT": "https://mock-agents.azure.com/",
    "AZURE_AI_PROJECT_ENDPOINT": "https://mock-project.azure.com/",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": (
        "InstrumentationKey=mock;IngestionEndpoint=https://mock-ingestion"
    ),
    "USER_LOCAL_BROWSER_LANGUAGE": "en-US",
}.items():
    os.environ.setdefault(_k, _v)

# Patch telemetry before importing app so configure_azure_monitor is never called
with patch("azure.monitor.opentelemetry.configure_azure_monitor", MagicMock()):
    from app import app  # noqa: E402  (must be after env setup)


# ---------------------------------------------------------------------------
# Helpers to build mock agent content objects
# ---------------------------------------------------------------------------


def _content(type_: str, **kwargs) -> SimpleNamespace:
    """Create a mock content object with .type and arbitrary attributes."""
    ns = SimpleNamespace(type=type_, **kwargs)
    # Ensure common optional attrs exist as None by default
    for attr in (
        "text",
        "message",
        "input",
        "output",
        "stderr",
        "stdout",
        "name",
        "tool_name",
        "arguments",
        "exception",
        "result",
        "annotations",
        "file_id",
        "additional_properties",
        "status",
        "server_name",
    ):
        if not hasattr(ns, attr):
            setattr(ns, attr, None)
    return ns


def _update(*contents) -> SimpleNamespace:
    """Wrap content objects in an agent update."""
    return SimpleNamespace(contents=list(contents))


def _annotation(file_id: str, filename: str, container_id: str) -> dict:
    """Build a code_interpreter annotation dict (agent_framework shape)."""
    return {
        "file_id": file_id,
        "url": filename,
        "additional_properties": {"container_id": container_id, "filename": filename},
    }


# ---------------------------------------------------------------------------
# Mock Cosmos service (in-memory, faithful to the real interface)
# ---------------------------------------------------------------------------


class _InMemoryCosmos:
    """Minimal in-memory replacement for ChatCosmosService."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        # Record every add_message call for assertions
        self.add_message_calls: list[dict] = []

    async def add_message(
        self, *, session_id, user_id, content, role="user", metadata=None
    ):
        self.add_message_calls.append(
            {
                "session_id": session_id,
                "user_id": user_id,
                "content": content,
                "role": role,
                "metadata": metadata or {},
            }
        )
        session = self._sessions.setdefault(
            session_id,
            {
                "id": session_id,
                "user_id": user_id,
                "messages": [],
            },
        )
        msg = {
            "id": str(uuid.uuid4()),
            "content": content,
            "role": role,
            "metadata": metadata or {},
        }
        session["messages"].append(msg)
        return msg

    async def get_session(self, session_id, user_id):
        return self._sessions.get(session_id)

    async def create_session(self, user_id, session_name=None):
        sid = str(uuid.uuid4())
        self._sessions[sid] = {"id": sid, "user_id": user_id, "messages": []}
        return self._sessions[sid]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cosmos_store():
    return _InMemoryCosmos()


@pytest.fixture
def session_id():
    return f"test-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse(body: bytes) -> list[dict]:
    """Parse raw SSE bytes into a list of event dicts."""
    events = []
    for line in body.decode().splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


# ===========================================================================
# 1. SSE stream – code_interpreter_tool_result with annotations
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sse_emits_generated_file_from_code_interpreter(cosmos_store, session_id):
    """
    The real event_stream() must emit a 'generated_file' SSE event and
    accumulate it in collected_generated_files when the agent produces a
    code_interpreter_tool_result with annotations.
    """
    file_id = "cfile_abc123"
    filename = "chart.png"
    container_id = "cnt_xyz789"

    ann = _annotation(file_id, filename, container_id)

    # Agent emits: text → code_interpreter_call → code_interpreter_result(annotation) → text
    async def _fake_invoke(message, file_ids=None):
        yield _update(
            _content("text", text="Running analysis..."),
        )
        yield _update(
            _content("code_interpreter_tool_call", input="import matplotlib"),
        )
        yield _update(
            _content(
                "code_interpreter_tool_result",
                output="Done",
                annotations=[ann],
            ),
        )
        yield _update(
            _content("text", text="Here is your chart."),
        )

    mock_agent = MagicMock()
    mock_agent.invoke = _fake_invoke
    mock_agent.open = AsyncMock()

    with (
        patch(
            "v4.api.router.get_chat_cosmos_service",
            new=AsyncMock(return_value=cosmos_store),
        ),
        patch(
            "v4.config.agent_pool.get_or_create",
            new=AsyncMock(return_value=mock_agent),
        ),
        patch(
            "v4.orchestration.intent_router.IntentRouter.classify_async",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    intent=SimpleNamespace(value="conversational"),
                    confidence=0.95,
                )
            ),
        ),
        patch(
            "v4.api.router.get_authenticated_user_details",
            return_value={
                "user_principal_id": "u-test",
                "tenant_id": "t-test",
                "access_token": "tok",
            },
        ),
        patch(
            "v4.api.router.track_event_if_configured",
            MagicMock(),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v4/chat/message/stream",
                json={"message": "Generate a chart", "session_id": session_id},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )

    assert response.status_code == 200, response.text
    events = _parse_sse(response.content)

    # ── Assert: SSE must contain a generated_file event ──────────────
    gf_events = [e for e in events if e.get("type") == "generated_file"]
    assert len(gf_events) == 1, (
        f"Expected 1 generated_file SSE event, got {len(gf_events)}.\n"
        f"All events: {events}"
    )

    gf = gf_events[0]
    assert gf["file_id"] == file_id, f"file_id mismatch: {gf}"
    assert gf["filename"] == filename, f"filename mismatch: {gf}"
    assert gf["container_id"] == container_id, f"container_id mismatch: {gf}"
    assert f"/api/v4/chat/download-file/{file_id}" in gf["download_url"], (
        f"download_url missing file_id: {gf}"
    )
    assert f"container_id={container_id}" in gf["download_url"], (
        f"download_url missing container_id: {gf}"
    )

    # ── Assert: token events carry the LLM text ──────────────────────
    token_events = [e for e in events if e.get("type") == "token"]
    full_text = "".join(e["content"] for e in token_events)
    assert "chart" in full_text.lower(), f"Expected LLM text in tokens: {token_events}"

    # ── Assert: done event present ────────────────────────────────────
    done_events = [e for e in events if e.get("type") == "done"]
    assert done_events, "Missing 'done' SSE event"


# ===========================================================================
# 2. SSE stream – collected_generated_files persisted to Cosmos
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_cosmos_persisted_with_generated_files_metadata(cosmos_store, session_id):
    """
    After the stream ends, chat_svc.add_message for the assistant message
    must include metadata['generated_files'] with the correct structure
    (no 'type' field, has file_id/filename/container_id/download_url).
    """
    file_id = "cfile_persist_test"
    filename = "analysis.csv"
    container_id = "cnt_persist"

    ann = _annotation(file_id, filename, container_id)

    async def _fake_invoke(message, file_ids=None):
        yield _update(_content("text", text="Done."))
        yield _update(
            _content("code_interpreter_tool_result", output="ok", annotations=[ann])
        )

    mock_agent = MagicMock()
    mock_agent.invoke = _fake_invoke
    mock_agent.open = AsyncMock()

    with (
        patch(
            "v4.api.router.get_chat_cosmos_service",
            new=AsyncMock(return_value=cosmos_store),
        ),
        patch(
            "v4.config.agent_pool.get_or_create", new=AsyncMock(return_value=mock_agent)
        ),
        patch(
            "v4.orchestration.intent_router.IntentRouter.classify_async",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    intent=SimpleNamespace(value="conversational"),
                    confidence=0.9,
                )
            ),
        ),
        patch(
            "v4.api.router.get_authenticated_user_details",
            return_value={
                "user_principal_id": "u-test",
                "tenant_id": "t-test",
                "access_token": "tok",
            },
        ),
        patch("v4.api.router.track_event_if_configured", MagicMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/api/v4/chat/message/stream",
                json={"message": "Analyse data", "session_id": session_id},
                headers={"Content-Type": "application/json"},
                timeout=30,
            )

    # Find the assistant persistence call (role="assistant")
    assistant_calls = [
        c for c in cosmos_store.add_message_calls if c["role"] == "assistant"
    ]
    assert assistant_calls, "No assistant message persisted to Cosmos"

    meta = assistant_calls[-1]["metadata"]
    assert "generated_files" in meta, (
        f"metadata missing 'generated_files' key. Got: {meta}"
    )

    gf_list = meta["generated_files"]
    assert len(gf_list) == 1, f"Expected 1 generated_file in metadata, got: {gf_list}"

    gf = gf_list[0]

    # Verify structure — no 'type' field (internal only)
    assert "type" not in gf, f"'type' key must NOT be persisted to Cosmos: {gf}"
    assert gf["file_id"] == file_id
    assert gf["filename"] == filename
    assert gf["container_id"] == container_id
    assert f"/api/v4/chat/download-file/{file_id}" in gf["download_url"]
    assert f"container_id={container_id}" in gf["download_url"]


# ===========================================================================
# 3. SSE stream – hosted_file content type
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sse_emits_generated_file_from_hosted_file_content(
    cosmos_store, session_id
):
    """
    The router must also emit 'generated_file' SSE events for hosted_file
    content type (not only code_interpreter_tool_result annotations).
    """
    file_id = "hosted_file_xyz"
    filename = "report.pdf"
    container_id = "cnt_hosted"

    async def _fake_invoke(message, file_ids=None):
        yield _update(
            _content("text", text="Generating report..."),
        )
        yield _update(
            _content(
                "hosted_file",
                file_id=file_id,
                additional_properties={
                    "container_id": container_id,
                    "filename": filename,
                },
            )
        )
        yield _update(_content("text", text="Report ready."))

    mock_agent = MagicMock()
    mock_agent.invoke = _fake_invoke
    mock_agent.open = AsyncMock()

    with (
        patch(
            "v4.api.router.get_chat_cosmos_service",
            new=AsyncMock(return_value=cosmos_store),
        ),
        patch(
            "v4.config.agent_pool.get_or_create", new=AsyncMock(return_value=mock_agent)
        ),
        patch(
            "v4.orchestration.intent_router.IntentRouter.classify_async",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    intent=SimpleNamespace(value="conversational"),
                    confidence=0.9,
                )
            ),
        ),
        patch(
            "v4.api.router.get_authenticated_user_details",
            return_value={
                "user_principal_id": "u-test",
                "tenant_id": "t-test",
                "access_token": "tok",
            },
        ),
        patch("v4.api.router.track_event_if_configured", MagicMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v4/chat/message/stream",
                json={"message": "Generate report", "session_id": session_id},
                timeout=30,
            )

    events = _parse_sse(response.content)
    gf_events = [e for e in events if e.get("type") == "generated_file"]

    assert len(gf_events) == 1, f"Expected 1 generated_file event: {events}"
    assert gf_events[0]["file_id"] == file_id
    assert gf_events[0]["filename"] == filename
    assert gf_events[0]["container_id"] == container_id


# ===========================================================================
# 4. SSE stream – no generated files → metadata has no generated_files key
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_no_generated_files_metadata_absent(cosmos_store, session_id):
    """
    When the agent produces no files, metadata must NOT include
    'generated_files' (prevents Cosmos pollution with empty lists).
    """

    async def _fake_invoke(message, file_ids=None):
        yield _update(_content("text", text="Hello world."))

    mock_agent = MagicMock()
    mock_agent.invoke = _fake_invoke
    mock_agent.open = AsyncMock()

    with (
        patch(
            "v4.api.router.get_chat_cosmos_service",
            new=AsyncMock(return_value=cosmos_store),
        ),
        patch(
            "v4.config.agent_pool.get_or_create", new=AsyncMock(return_value=mock_agent)
        ),
        patch(
            "v4.orchestration.intent_router.IntentRouter.classify_async",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    intent=SimpleNamespace(value="conversational"),
                    confidence=0.9,
                )
            ),
        ),
        patch(
            "v4.api.router.get_authenticated_user_details",
            return_value={
                "user_principal_id": "u-test",
                "tenant_id": "t-test",
                "access_token": "tok",
            },
        ),
        patch("v4.api.router.track_event_if_configured", MagicMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/api/v4/chat/message/stream",
                json={"message": "Hello", "session_id": session_id},
                timeout=30,
            )

    assistant_calls = [
        c for c in cosmos_store.add_message_calls if c["role"] == "assistant"
    ]
    assert assistant_calls, "No assistant message persisted"
    meta = assistant_calls[-1]["metadata"]
    assert "generated_files" not in meta, (
        f"'generated_files' must be absent when no files generated. Got: {meta}"
    )


# ===========================================================================
# 5. Download endpoint – cfile_ path (container files)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_download_cfile_returns_file_content():
    """
    GET /api/v4/chat/download-file/cfile_xxx?container_id=cnt_yyy
    must call openai.containers.files.retrieve + content.retrieve
    and return the file bytes with the correct Content-Type.
    """
    file_id = "cfile_download_test"
    container_id = "cnt_dl_test"
    file_bytes = b"PNG\x89fake-png-data"

    # Mock chain: app_config.get_ai_project_client()
    #   → project.get_openai_client() → openai
    #   → openai.containers.files.retrieve → file_info (path attr)
    #   → openai.containers.files.content.retrieve → content_obj
    #   → content_obj.aread() → bytes
    #   → openai.close()
    #   → project.close()

    content_obj = MagicMock()
    content_obj.aread = AsyncMock(return_value=file_bytes)

    mock_files_content = MagicMock()
    mock_files_content.retrieve = AsyncMock(return_value=content_obj)

    file_info = SimpleNamespace(path=f"/mnt/data/{file_id}.png")

    mock_files = MagicMock()
    mock_files.retrieve = AsyncMock(return_value=file_info)
    mock_files.content = mock_files_content

    mock_containers = MagicMock()
    mock_containers.files = mock_files

    mock_openai = MagicMock()
    mock_openai.containers = mock_containers
    mock_openai.close = AsyncMock()

    mock_project = MagicMock()
    mock_project.get_openai_client = MagicMock(return_value=mock_openai)
    mock_project.close = AsyncMock()

    with (
        patch(
            "common.config.app_config.AppConfig.get_ai_project_client",
            return_value=mock_project,
        ),
        patch(
            "v4.api.router.get_authenticated_user_details",
            return_value={"user_principal_id": "u-test", "tenant_id": "t-test"},
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/v4/chat/download-file/{file_id}",
                params={"container_id": container_id},
                timeout=30,
            )

    assert response.status_code == 200, response.text
    assert response.content == file_bytes, "File bytes mismatch"
    assert "image/png" in response.headers["content-type"], (
        f"Expected image/png, got: {response.headers.get('content-type')}"
    )
    assert response.headers["content-length"] == str(len(file_bytes))

    # Verify the correct Azure calls were made
    mock_files.retrieve.assert_awaited_once_with(
        file_id=file_id, container_id=container_id
    )
    mock_files_content.retrieve.assert_awaited_once_with(
        file_id=file_id, container_id=container_id
    )
    content_obj.aread.assert_awaited_once()
    mock_openai.close.assert_awaited_once()
    mock_project.close.assert_awaited_once()


# ===========================================================================
# 6. Download endpoint – cfile_ without container_id → 400
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_download_cfile_missing_container_id_returns_400():
    """
    GET /api/v4/chat/download-file/cfile_xxx without container_id
    must return 400 (not 500) before calling any Azure API.
    """
    with patch(
        "v4.api.router.get_authenticated_user_details",
        return_value={"user_principal_id": "u-test", "tenant_id": "t-test"},
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v4/chat/download-file/cfile_missing",
                timeout=10,
            )

    assert response.status_code == 400, response.text
    assert "container_id" in response.json()["detail"].lower()


# ===========================================================================
# 7. Download endpoint – regular file_id path (AgentsClient)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_download_regular_file_id_uses_agents_client():
    """
    GET /api/v4/chat/download-file/<regular_id> (no cfile_ prefix)
    must use AgentsClient.files.get + files.get_content, return bytes.
    """
    file_id = "file-regular-abc"
    file_bytes = b"hello from python script\n"

    async def _fake_content_stream():
        yield file_bytes[:10]
        yield file_bytes[10:]

    mock_file_info = SimpleNamespace(filename="script.py")

    mock_files = MagicMock()
    mock_files.get = AsyncMock(return_value=mock_file_info)
    mock_files.get_content = AsyncMock(return_value=_fake_content_stream())

    mock_agents_client = MagicMock()
    mock_agents_client.files = mock_files
    mock_agents_client.__aenter__ = AsyncMock(return_value=mock_agents_client)
    mock_agents_client.__aexit__ = AsyncMock(return_value=False)

    mock_cred = MagicMock()

    with (
        patch(
            "common.config.app_config.AppConfig.get_azure_credential_async",
            return_value=mock_cred,
        ),
        patch(
            "azure.ai.agents.aio.AgentsClient",
            return_value=mock_agents_client,
        ),
        patch(
            "v4.api.router.get_authenticated_user_details",
            return_value={"user_principal_id": "u-test", "tenant_id": "t-test"},
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/v4/chat/download-file/{file_id}",
                timeout=30,
            )

    assert response.status_code == 200, response.text
    assert response.content == file_bytes
    assert (
        "text/x-python" in response.headers["content-type"]
        or "text/plain" in response.headers["content-type"]
        or "application/octet-stream" in response.headers["content-type"]
    ), f"Unexpected content-type: {response.headers.get('content-type')}"
    mock_files.get.assert_awaited_once_with(file_id)
    mock_files.get_content.assert_awaited_once_with(file_id)


# ===========================================================================
# 8. Multiple files in one stream – all accumulated and persisted
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_multiple_files_all_accumulated(cosmos_store, session_id):
    """
    When the agent generates multiple files (e.g. plot.png + data.csv),
    ALL must appear in:
      - SSE generated_file events (one per file)
      - Cosmos metadata['generated_files'] list
    """
    files = [
        ("cfile_plot", "plot.png", "cnt_multi"),
        ("cfile_data", "data.csv", "cnt_multi"),
    ]

    async def _fake_invoke(message, file_ids=None):
        yield _update(_content("text", text="Processing..."))
        yield _update(
            _content(
                "code_interpreter_tool_result",
                output="done",
                annotations=[_annotation(*f) for f in files],
            )
        )
        yield _update(_content("text", text="All files ready."))

    mock_agent = MagicMock()
    mock_agent.invoke = _fake_invoke
    mock_agent.open = AsyncMock()

    with (
        patch(
            "v4.api.router.get_chat_cosmos_service",
            new=AsyncMock(return_value=cosmos_store),
        ),
        patch(
            "v4.config.agent_pool.get_or_create", new=AsyncMock(return_value=mock_agent)
        ),
        patch(
            "v4.orchestration.intent_router.IntentRouter.classify_async",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    intent=SimpleNamespace(value="conversational"),
                    confidence=0.9,
                )
            ),
        ),
        patch(
            "v4.api.router.get_authenticated_user_details",
            return_value={
                "user_principal_id": "u-test",
                "tenant_id": "t-test",
                "access_token": "tok",
            },
        ),
        patch("v4.api.router.track_event_if_configured", MagicMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v4/chat/message/stream",
                json={"message": "Generate charts", "session_id": session_id},
                timeout=30,
            )

    events = _parse_sse(response.content)
    gf_events = [e for e in events if e.get("type") == "generated_file"]
    assert len(gf_events) == 2, f"Expected 2 generated_file events, got: {gf_events}"

    emitted_ids = {e["file_id"] for e in gf_events}
    assert emitted_ids == {"cfile_plot", "cfile_data"}

    # Cosmos check
    assistant_calls = [
        c for c in cosmos_store.add_message_calls if c["role"] == "assistant"
    ]
    meta = assistant_calls[-1]["metadata"]
    assert len(meta["generated_files"]) == 2
    persisted_ids = {gf["file_id"] for gf in meta["generated_files"]}
    assert persisted_ids == {"cfile_plot", "cfile_data"}


# ===========================================================================
# 9. Inline: sandbox link in SSE token stream (no rewrite here — router only)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sandbox_links_remain_in_token_stream(cosmos_store, session_id):
    """
    The router does NOT strip sandbox: links — that is the frontend's job.
    Verify the raw token text passes through unchanged so the frontend
    actually receives the link to process.

    If this test fails it means the backend stripped links prematurely,
    which would break the frontend rewrite pipeline.
    """
    sandbox_text = (
        "Here is your file: [sandbox:/mnt/data/chart.png](sandbox:/mnt/data/chart.png)"
    )

    async def _fake_invoke(message, file_ids=None):
        yield _update(_content("text", text=sandbox_text))

    mock_agent = MagicMock()
    mock_agent.invoke = _fake_invoke
    mock_agent.open = AsyncMock()

    with (
        patch(
            "v4.api.router.get_chat_cosmos_service",
            new=AsyncMock(return_value=cosmos_store),
        ),
        patch(
            "v4.config.agent_pool.get_or_create", new=AsyncMock(return_value=mock_agent)
        ),
        patch(
            "v4.orchestration.intent_router.IntentRouter.classify_async",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    intent=SimpleNamespace(value="conversational"),
                    confidence=0.9,
                )
            ),
        ),
        patch(
            "v4.api.router.get_authenticated_user_details",
            return_value={
                "user_principal_id": "u-test",
                "tenant_id": "t-test",
                "access_token": "tok",
            },
        ),
        patch("v4.api.router.track_event_if_configured", MagicMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v4/chat/message/stream",
                json={"message": "Make chart", "session_id": session_id},
                timeout=30,
            )

    events = _parse_sse(response.content)
    token_events = [e for e in events if e.get("type") == "token"]
    all_tokens = "".join(e["content"] for e in token_events)

    assert "sandbox:/mnt/data/chart.png" in all_tokens, (
        "Router must NOT strip sandbox: links — frontend handles rewrite.\n"
        f"Received tokens: {all_tokens!r}"
    )
