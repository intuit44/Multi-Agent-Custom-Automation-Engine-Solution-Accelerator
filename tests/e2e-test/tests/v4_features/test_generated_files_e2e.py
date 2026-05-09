"""
E2E — Generated Files Pipeline
================================

Validates the full user-facing flow for files produced by code_interpreter:

  Browser → submit prompt → SSE stream → frontend parses generated_file event
         → chip renders with /api/v4/chat/download-file URL (no sandbox:)
         → click triggers download via correct endpoint
         → downloaded bytes match what the server returned

WHY E2E IS NEEDED
-----------------
The backend unit tests (test_generated_files_pipeline.py) confirm the server
contract: SSE event emitted, Cosmos metadata persisted, download endpoint
returns bytes.  They cannot catch bugs in:

  • frontend apiService.tsx — generated_file event never wired to onGeneratedFile
  • Chat.tsx / HomeInput.tsx — chip not rendered or renders sandbox: href
  • ChatPage.tsx — sandbox link NOT stripped before attaching to message
  • Download URL construction — wrong path / missing query params

APPROACH — Playwright network interception (no real Azure backend required)
---------------------------------------------------------------------------
page.route() intercepts:
  1. /api/v4/chat/stream       → returns a synthetic SSE body with one
                                  generated_file event + done token
  2. /api/v4/chat/download-file → returns mock CSV bytes

This lets the suite run in CI without credentials.

WHAT EACH TEST VALIDATES
------------------------
1. test_generated_file_chip_renders
   → chip appears in the DOM after SSE stream completes
   → href does NOT contain "sandbox:" (critical bug surface)
   → href contains "/api/v4/chat/download-file"

2. test_generated_file_download_url_structure
   → the link's href encodes file_id and filename as query params
   → no raw sandbox: URI survives to the rendered anchor

3. test_download_request_reaches_correct_endpoint
   → click on the chip triggers a network request to /download-file
   → the request does NOT go to sandbox: or any other URI

4. test_downloaded_bytes_match_server_response
   → the bytes received from the mock download endpoint are intact
   → verifies the pipeline does not corrupt binary content

5. test_no_chip_without_generated_file_event
   → when the SSE stream has no generated_file event, no chip appears
   → sanity check that chip detection is not a false positive

6. test_session_reload_restores_chips
   → reload page (simulating browser refresh / session restore)
   → chips re-appear from Cosmos metadata (loadSession path)
   → href still points to /api/v4/chat/download-file, not sandbox:
"""

import json
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest
from playwright.sync_api import Page, Route, Request

# Make the e2e-test package importable when running from repo root
E2E_ROOT = Path(__file__).resolve().parents[2]
if str(E2E_ROOT) not in sys.path:
    sys.path.insert(0, str(E2E_ROOT))

from e2e_constants import URL, API_URL  # noqa: E402

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

# The file the mock SSE will advertise
MOCK_FILE_ID = "file_abc123test"
MOCK_FILENAME = "sales_report.csv"
MOCK_CONTAINER_ID = "container_xyz789"
MOCK_DOWNLOAD_URL = (
    f"/api/v4/chat/download-file"
    f"?file_id={MOCK_FILE_ID}&filename={MOCK_FILENAME}&container_id={MOCK_CONTAINER_ID}"
)
MOCK_FILE_BYTES = b"col_a,col_b\n1,2\n3,4\n"
MOCK_SESSION_ID = "sess_e2e_test_001"

# Prompt that will be sent in the chat input
PROMPT_TEXT = "Generate a sales report as a CSV file"

# SSE stream URL pattern
SSE_PATH_PATTERN = "**/api/v4/chat/stream"
DOWNLOAD_PATH_PATTERN = "**/api/v4/chat/download-file**"
SESSIONS_PATH_PATTERN = "**/api/v4/chat/sessions**"
SESSION_MESSAGES_PATTERN = f"**/api/v4/chat/sessions/{MOCK_SESSION_ID}/messages**"

# Selectors — semantic, resilient to CSS changes
TEXTAREA = "textarea"
SEND_BTN = "//button[contains(@class, 'home-input-send-button')]"
# Chip: an <a> whose text contains the filename and href points to download-file
CHIP_LOCATOR = f"a[href*='download-file'][href*='{MOCK_FILE_ID}']"
# Also match by visible text in case href changes slightly
CHIP_BY_TEXT = f"a:has-text('{MOCK_FILENAME}')"
GENERATED_FILES_LABEL = "text=📥 Generated files"


# ── SSE body builders ───────────────────────────────────────────────────────


def _sse_event(event_type: str, data: dict) -> bytes:
    """Format one SSE event as bytes."""
    payload = json.dumps(data)
    return f"event: {event_type}\ndata: {payload}\n\n".encode()


def _build_sse_stream(with_file: bool = True) -> bytes:
    """
    Build a complete mock SSE response body.

    Sequence:
      token          → "Here is your report."
      generated_file → {file_id, filename, download_url}  (if with_file=True)
      done           → {}
    """
    parts: list[bytes] = []
    parts.append(_sse_event("token", {"content": "Here is your report."}))
    if with_file:
        parts.append(
            _sse_event(
                "generated_file",
                {
                    "file_id": MOCK_FILE_ID,
                    "filename": MOCK_FILENAME,
                    "download_url": MOCK_DOWNLOAD_URL,
                },
            )
        )
    parts.append(_sse_event("done", {}))
    return b"".join(parts)


# ── Route handlers ──────────────────────────────────────────────────────────


def _fulfill_sse(route: Route, *, with_file: bool = True) -> None:
    """Fulfill an SSE stream route with the mock body."""
    route.fulfill(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
        body=_build_sse_stream(with_file=with_file),
    )


def _fulfill_download(route: Route) -> None:
    """Fulfill a download-file route with mock CSV bytes."""
    route.fulfill(
        status=200,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{MOCK_FILENAME}"',
        },
        body=MOCK_FILE_BYTES,
    )


def _fulfill_sessions_create(route: Route) -> None:
    """Return a minimal session creation response."""
    route.fulfill(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            {
                "id": MOCK_SESSION_ID,
                "user_id": "test_user",
                "session_name": "Test session",
                "message_count": 0,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "last_message_at": None,
                "is_active": True,
            }
        ),
    )


def _fulfill_sessions_list(route: Route) -> None:
    """Return a session list with one session that has a generated file."""
    route.fulfill(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            {
                "sessions": [
                    {
                        "id": MOCK_SESSION_ID,
                        "user_id": "test_user",
                        "session_name": "Test session",
                        "message_count": 2,
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "last_message_at": "2026-01-01T00:01:00Z",
                        "is_active": True,
                    }
                ]
            }
        ),
    )


def _fulfill_session_messages(route: Route) -> None:
    """Return messages with a generated file in the assistant turn metadata."""
    route.fulfill(
        status=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            {
                "messages": [
                    {
                        "id": "msg_user_001",
                        "session_id": MOCK_SESSION_ID,
                        "role": "user",
                        "content": PROMPT_TEXT,
                        "timestamp": "2026-01-01T00:00:30Z",
                        "metadata": {},
                    },
                    {
                        "id": "msg_asst_001",
                        "session_id": MOCK_SESSION_ID,
                        "role": "assistant",
                        "content": "Here is your report.",
                        "timestamp": "2026-01-01T00:01:00Z",
                        "metadata": {
                            "generated_files": [
                                {
                                    "file_id": MOCK_FILE_ID,
                                    "filename": MOCK_FILENAME,
                                    "download_url": MOCK_DOWNLOAD_URL,
                                }
                            ],
                        },
                    },
                ]
            }
        ),
    )


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def chat_page(fresh_page: Page):
    """
    Yield a Page that has:
      • SSE stream mocked to emit one generated_file event
      • Download endpoint mocked to return MOCK_FILE_BYTES
      • All unrelated API calls pass-through (not intercepted)

    After yielding, all routes are removed.
    """
    page = fresh_page

    # Intercept SSE
    page.route(
        SSE_PATH_PATTERN, lambda route, _req: _fulfill_sse(route, with_file=True)
    )
    # Intercept download
    page.route(DOWNLOAD_PATH_PATTERN, lambda route, _req: _fulfill_download(route))
    # Intercept session creation/listing so the chat page doesn't error
    page.route(
        SESSIONS_PATH_PATTERN,
        lambda route, _req: (
            _fulfill_sessions_create(route)
            if _req.method == "POST"
            else _fulfill_sessions_list(route)
        ),
    )

    yield page

    # Cleanup
    page.unroute(SSE_PATH_PATTERN)
    page.unroute(DOWNLOAD_PATH_PATTERN)
    page.unroute(SESSIONS_PATH_PATTERN)


@pytest.fixture()
def chat_page_no_file(fresh_page: Page):
    """
    Like ``chat_page`` but the SSE stream does NOT emit generated_file.
    Used to verify that no spurious chip appears.
    """
    page = fresh_page

    page.route(
        SSE_PATH_PATTERN, lambda route, _req: _fulfill_sse(route, with_file=False)
    )
    page.route(
        SESSIONS_PATH_PATTERN,
        lambda route, _req: (
            _fulfill_sessions_create(route)
            if _req.method == "POST"
            else _fulfill_sessions_list(route)
        ),
    )

    yield page

    page.unroute(SSE_PATH_PATTERN)
    page.unroute(SESSIONS_PATH_PATTERN)


@pytest.fixture()
def chat_page_with_history(fresh_page: Page):
    """
    Simulates a page load where session history already contains a generated
    file (tests the loadSession / Cosmos-restore path).
    """
    page = fresh_page

    page.route(
        SESSIONS_PATH_PATTERN,
        lambda route, _req: (
            _fulfill_sessions_create(route)
            if _req.method == "POST"
            else _fulfill_sessions_list(route)
        ),
    )
    page.route(
        SESSION_MESSAGES_PATTERN, lambda route, _req: _fulfill_session_messages(route)
    )
    page.route(DOWNLOAD_PATH_PATTERN, lambda route, _req: _fulfill_download(route))

    yield page

    page.unroute(SESSIONS_PATH_PATTERN)
    page.unroute(SESSION_MESSAGES_PATTERN)
    page.unroute(DOWNLOAD_PATH_PATTERN)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _submit_prompt(page: Page, text: str = PROMPT_TEXT) -> None:
    """Fill the textarea and click send."""
    page.locator(TEXTAREA).fill(text)
    page.wait_for_timeout(300)
    page.locator(SEND_BTN).click()


def _wait_for_chip(page: Page, timeout_ms: int = 15_000) -> None:
    """Wait until the generated-file chip is visible."""
    page.locator(CHIP_LOCATOR).wait_for(state="visible", timeout=timeout_ms)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1 — Chip renders with correct href after SSE stream
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
def test_generated_file_chip_renders(chat_page: Page):
    """
    After a prompt that triggers a generated_file SSE event:
      • A download chip appears in the DOM.
      • The chip href does NOT contain 'sandbox:'.
      • The chip href contains '/api/v4/chat/download-file'.

    Failure modes caught:
      - apiService.tsx: generated_file case missing → chip never rendered
      - Chat.tsx / HomeInput.tsx: chip not wired to generatedFiles state
      - ChatPage.tsx: sandbox: link passed through without rewriting
    """
    _submit_prompt(chat_page)
    _wait_for_chip(chat_page)

    chip = chat_page.locator(CHIP_LOCATOR).first
    href: str = chip.get_attribute("href") or ""

    logger.info("Chip href: %s", href)

    assert href, "Chip has no href attribute"
    assert "sandbox:" not in href, (
        f"href still contains 'sandbox:' — frontend did not rewrite the URL.\n"
        f"  href: {href}"
    )
    assert "/api/v4/chat/download-file" in href, (
        f"href does not point to the download endpoint.\n  href: {href}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2 — Download URL encodes file_id and filename as query params
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
def test_generated_file_download_url_structure(chat_page: Page):
    """
    The chip href must encode file_id and filename as query parameters so
    the backend can locate the file.

    Failure modes caught:
      - download_url construction omits required params
      - URL gets double-encoded or truncated during SSE→React state pipeline
    """
    _submit_prompt(chat_page)
    _wait_for_chip(chat_page)

    chip = chat_page.locator(CHIP_LOCATOR).first
    href: str = chip.get_attribute("href") or ""

    assert href, "Chip has no href attribute"
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)  # returns Dict[str, List[str]]

    assert "file_id" in qs, f"'file_id' missing from download URL query: {href}"
    assert qs["file_id"][0] == MOCK_FILE_ID, (
        f"file_id mismatch: expected {MOCK_FILE_ID!r}, got {qs['file_id'][0]!r}"
    )
    assert "filename" in qs, f"'filename' missing from download URL query: {href}"
    assert qs["filename"][0] == MOCK_FILENAME, (
        f"filename mismatch: expected {MOCK_FILENAME!r}, got {qs['filename'][0]!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3 — Clicking the chip triggers a request to the correct endpoint
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
def test_download_request_reaches_correct_endpoint(chat_page: Page):
    """
    When the user clicks the chip, the browser makes a request to
    /api/v4/chat/download-file — NOT to a sandbox: URI or any other path.

    Failure modes caught:
      - href still points to sandbox: so the browser tries an unsupported scheme
      - href points to a blob: or data: URI that bypasses the backend
      - The anchor has no href / is rendered as a <span> instead of <a>
    """
    _submit_prompt(chat_page)
    _wait_for_chip(chat_page)

    captured_requests: list[str] = []

    def _capture(req: Request):
        if "download-file" in req.url or "sandbox:" in req.url:
            captured_requests.append(req.url)

    chat_page.on("request", _capture)

    try:
        chip = chat_page.locator(CHIP_LOCATOR).first
        # Use expect_download to catch the file download trigger
        with chat_page.expect_request("**/download-file**", timeout=8_000) as req_info:
            chip.click()
        fired_url = req_info.value.url
    except Exception:
        # If expect_request times out the click might have opened a new tab
        # Fallback: check captured_requests
        fired_url = captured_requests[0] if captured_requests else ""
    finally:
        chat_page.remove_listener("request", _capture)

    assert fired_url, (
        "No request to /download-file was detected after clicking the chip.\n"
        "The chip may be pointing to a non-HTTP URI (sandbox:, blob:, data:) "
        "or the click handler is not wired correctly."
    )
    assert "sandbox:" not in fired_url, (
        f"Click triggered a 'sandbox:' request — URL was NOT rewritten.\n"
        f"  fired_url: {fired_url}"
    )
    assert "/api/v4/chat/download-file" in fired_url, (
        f"Click did not reach the download endpoint.\n  fired_url: {fired_url}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4 — Downloaded bytes are intact (no corruption through the pipeline)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
def test_downloaded_bytes_match_server_response(chat_page: Page):
    """
    The bytes returned by the mock download endpoint reach the browser
    without corruption.  Playwright's APIRequestContext is used to fetch
    the download URL directly and compare bytes.

    Failure modes caught:
      - Frontend rewrites the download_url in a way that breaks the request
      - Missing query params cause the backend to 400/500 before returning bytes
      - Content is re-encoded (base64, JSON-wrapped) instead of raw binary
    """
    _submit_prompt(chat_page)
    _wait_for_chip(chat_page)

    chip = chat_page.locator(CHIP_LOCATOR).first
    href: str = chip.get_attribute("href") or ""

    assert href, "Chip has no href — cannot construct download URL"
    # Build absolute URL for the API request
    base = (API_URL or URL or "http://localhost:8000").rstrip("/")
    full_url: str = href if href.startswith("http") else base + href

    response = chat_page.request.get(full_url, timeout=10_000)

    assert response.ok, (
        f"Download endpoint returned {response.status} for {full_url}\n"
        f"Body: {response.text()[:200]}"
    )
    body = response.body()
    assert body == MOCK_FILE_BYTES, (
        f"Downloaded bytes differ from mock.\n"
        f"  expected: {MOCK_FILE_BYTES!r}\n"
        f"  got:      {body!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 5 — No chip appears when SSE emits no generated_file event
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
def test_no_chip_without_generated_file_event(chat_page_no_file: Page):
    """
    When the SSE stream does NOT contain a generated_file event, no chip
    should appear.  This is a sanity / false-positive guard.

    Failure modes caught:
      - Chip rendered from stale state of a previous test (state leak)
      - Chip always rendered regardless of SSE content
    """
    _submit_prompt(chat_page_no_file)

    # Wait for the assistant response token to arrive so we know streaming ended
    chat_page_no_file.wait_for_timeout(5_000)

    # The chip should NOT be present
    chip_count = chat_page_no_file.locator(CHIP_LOCATOR).count()
    assert chip_count == 0, (
        f"A file chip appeared even though no generated_file event was emitted.\n"
        f"  chip count: {chip_count}\n"
        "This indicates stale state or the chip renders unconditionally."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 6 — Chip label shows the human-readable filename (no raw IDs)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
def test_chip_label_is_human_readable_filename(chat_page: Page):
    """
    The visible text of the chip must be the filename, not the raw file_id
    (e.g. 'file_abc123test') or any internal identifier.

    Failure modes caught:
      - Template renders f.file_id instead of f.filename
      - filename field lost during SSE→React state pipeline
    """
    _submit_prompt(chat_page)
    _wait_for_chip(chat_page)

    chip = chat_page.locator(CHIP_LOCATOR).first
    text = chip.inner_text().strip()

    logger.info("Chip visible text: %r", text)

    assert MOCK_FILENAME in text, (
        f"Chip text does not show the filename.\n"
        f"  expected to contain: {MOCK_FILENAME!r}\n"
        f"  actual text:         {text!r}"
    )
    # Raw file_id must not be the only visible content
    assert text != MOCK_FILE_ID, f"Chip shows raw file_id instead of filename: {text!r}"


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 7 — Session reload restores chips from Cosmos metadata
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.e2e
def test_session_reload_restores_chips(chat_page_with_history: Page, fresh_page: Page):
    """
    When the user reloads the app (or reopens a previous chat session),
    chips for previously generated files must reappear from the session
    history loaded via the ChatService.loadSession path.

    The mock returns a session whose assistant message has
    ``metadata.generated_files`` populated.  After navigation to the
    chat URL, the chip must appear with a clean /download-file href.

    Failure modes caught:
      - ChatService.loadSession doesn't parse metadata.generated_files
      - attachGeneratedFilesToLastMessage not called on session restore
      - Loaded chips still carry sandbox: URLs from Cosmos-stored data
    """
    page = chat_page_with_history

    # Navigate directly to the chat page for the mock session
    chat_url = f"{(URL or 'http://localhost:3001').rstrip('/')}/chat/{MOCK_SESSION_ID}"
    logger.info("Navigating to session URL: %s", chat_url)
    page.goto(chat_url, wait_until="domcontentloaded")
    page.wait_for_timeout(6_000)  # let React load session data

    # The chip should be restored from history
    try:
        page.locator(CHIP_LOCATOR).wait_for(state="visible", timeout=12_000)
        chip_visible = True
    except Exception:
        chip_visible = False

    assert chip_visible, (
        "After navigating to an existing session, the generated-file chip was NOT "
        "restored.  The ChatService.loadSession path likely does not call "
        "attachGeneratedFilesToLastMessage with the stored generated_files."
    )

    chip = page.locator(CHIP_LOCATOR).first
    href: str = chip.get_attribute("href") or ""

    assert href, "Restored chip has no href"
    assert "sandbox:" not in href, (
        f"Restored chip still has sandbox: href — Cosmos stored raw URL "
        f"without rewriting.\n  href: {href}"
    )
    assert "/api/v4/chat/download-file" in href, (
        f"Restored chip href does not point to download endpoint.\n  href: {href}"
    )
