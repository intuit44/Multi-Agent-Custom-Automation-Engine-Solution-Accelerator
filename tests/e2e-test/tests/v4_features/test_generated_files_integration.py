"""
E2E Integration — Generated Files Pipeline (REAL BACKEND)
==========================================================

These tests exercise the COMPLETE production stack without any network mocks:

  Browser (Playwright)
    → React frontend
      → /api/v4/chat/stream  (real FastAPI backend)
        → Azure AI Agent Service
          → code_interpreter tool
            → file generated
              → SSE: generated_file event
                → frontend: chip rendered, href=/api/v4/chat/download-file
                  → click: real download request
                    → backend: bytes returned
                      → Cosmos DB: metadata.generated_files persisted
                        → page reload: chip restored from session history

WHAT THIS CATCHES THAT UNIT TESTS CANNOT
-----------------------------------------
| Failure mode                           | Backend unit | Mock E2E | THIS |
|---------------------------------------|:------------:|:--------:|:----:|
| agent never calls code_interpreter    |      ✗       |    ✗     |  ✓   |
| SSE event malformed by real stream    |      ✗       |    ✗     |  ✓   |
| Azure credential expired / wrong scope|      ✗       |    ✗     |  ✓   |
| Cosmos write fails in prod config     |      ✗       |    ✗     |  ✓   |
| frontend drops event under real load  |      ✗       |    ✗     |  ✓   |
| download endpoint needs auth header   |      ✗       |    ✗     |  ✓   |

PREREQUISITES
-------------
Set these env vars (or populate .env in tests/e2e-test/):

  MACAE_WEB_URL       — frontend origin, e.g. http://localhost:3001
  MACAE_URL_API       — backend origin,  e.g. http://localhost:8000
  AZURE_CLIENT_ID     — service principal with access to the agent project
  AZURE_TENANT_ID
  AZURE_CLIENT_SECRET (or certificate / managed identity as needed)

MARKS
-----
  @pytest.mark.e2e          → requires running frontend (Playwright)
  @pytest.mark.integration  → requires real Azure backend + credentials

Run only this suite:
  uv run pytest -m "e2e and integration" tests/e2e-test/ -v

Skip in CI that has no credentials:
  uv run pytest -m "not integration" tests/e2e-test/ -v
"""

import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

import pytest
import requests  # direct API assertions outside Playwright
from playwright.sync_api import Page

# ── path setup ────────────────────────────────────────────────────────────────
E2E_ROOT = Path(__file__).resolve().parents[2]
if str(E2E_ROOT) not in sys.path:
    sys.path.insert(0, str(E2E_ROOT))

from e2e_constants import URL, API_URL  # noqa: E402

logger = logging.getLogger(__name__)

# ── Selectors ─────────────────────────────────────────────────────────────────
TEXTAREA = "textarea"
SEND_BTN = "//button[contains(@class, 'home-input-send-button')]"
CONTOSO_LOGO = "//span[.='Contoso']"

# ── Config ────────────────────────────────────────────────────────────────────
# Prompt that reliably triggers code_interpreter in the configured agent(s).
# Override via env var to match your deployment's agent instructions.
CODEGEN_PROMPT = os.getenv(
    "E2E_CODEGEN_PROMPT",
    "Use the code interpreter to generate a CSV file with 3 rows of sample sales data "
    "and save it as sales_data.csv",
)

# How long to wait for the agent to finish (code_interpreter can be slow)
AGENT_TIMEOUT_MS = int(os.getenv("E2E_AGENT_TIMEOUT_MS", "120000"))  # 2 min default

# Session URL template — adjust if your frontend routing differs
CHAT_SESSION_URL_TMPL = "{base}/chat/{session_id}"

# ── Skip guard ────────────────────────────────────────────────────────────────


def _integration_available() -> bool:
    """Return True only when the required env vars are present."""
    required = ["MACAE_WEB_URL", "MACAE_URL_API"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        logger.warning(
            "Integration E2E skipped — missing env vars: %s", ", ".join(missing)
        )
        return False
    return True


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.skipif(
        not _integration_available(),
        reason=(
            "Integration E2E requires MACAE_WEB_URL + MACAE_URL_API env vars. "
            "Run 'export MACAE_WEB_URL=... MACAE_URL_API=...' before pytest."
        ),
    ),
]

# ── Helpers ───────────────────────────────────────────────────────────────────


def _api(path: str) -> str:
    """Build an absolute API URL."""
    base = (API_URL or "http://localhost:8000").rstrip("/")
    return f"{base}{path}"


def _frontend(path: str = "") -> str:
    base = (URL or "http://localhost:3001").rstrip("/")
    return f"{base}{path}"


def _submit_prompt(page: Page, text: str) -> None:
    """Fill the textarea and click Send."""
    page.locator(TEXTAREA).fill(text)
    page.wait_for_timeout(300)
    page.locator(SEND_BTN).click()


def _wait_for_chip(page: Page, timeout_ms: int = AGENT_TIMEOUT_MS) -> str:
    """
    Wait for a generated-file chip to appear and return its href.
    Polls every second so we get a useful elapsed-time log.
    """
    chip_selector = "a[href*='download-file']"
    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        chips = page.locator(chip_selector)
        if chips.count() > 0:
            elapsed = int((time.time() - start) * 1000)
            logger.info("Chip appeared after %d ms", elapsed)
            return chips.first.get_attribute("href") or ""
        page.wait_for_timeout(1000)
    raise AssertionError(
        f"No generated-file chip appeared within {timeout_ms} ms.\n"
        f"  prompt: {CODEGEN_PROMPT!r}\n"
        "  Possible causes:\n"
        "    • Agent did not call code_interpreter\n"
        "    • SSE generated_file event was emitted but frontend dropped it\n"
        "    • Frontend apiService.tsx onGeneratedFile callback not wired\n"
        "    • Chat.tsx / HomeInput.tsx conditional rendering not reached"
    )


def _current_session_id(page: Page) -> str:
    """Extract session ID from the current URL (/chat/<session_id>)."""
    m = re.search(r"/chat/([^/?#]+)", page.url)
    if not m:
        raise AssertionError(
            f"Cannot extract session_id from URL: {page.url}\n"
            "Expected pattern: .../chat/<session_id>"
        )
    return m.group(1)


def _cosmos_messages(session_id: str) -> list[dict[str, Any]]:
    """
    Fetch messages for *session_id* directly from the backend API.
    This exercises the same Cosmos read-path the frontend uses on reload.
    """
    url = _api(f"/api/v4/chat/sessions/{session_id}/messages")
    resp = requests.get(url, timeout=30)
    assert resp.status_code == 200, (
        f"GET {url} returned {resp.status_code}: {resp.text[:300]}"
    )
    return resp.json().get("messages", [])


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 1 — Chat flow: prompt → agent → SSE → chip visible
# ═══════════════════════════════════════════════════════════════════════════════


def test_chat_generates_file_and_chip_appears(fresh_page: Page):
    """
    Full chat flow validation:

      1. User sends a prompt that triggers code_interpreter.
      2. Agent produces a file.
      3. Backend emits a real SSE generated_file event.
      4. Frontend renders a download chip.
      5. Chip href → /api/v4/chat/download-file (no sandbox:).

    Failure modes:
      - Agent never calls code_interpreter (system prompt / routing issue)
      - SSE event not emitted by router.py event_stream()
      - apiService.tsx generated_file case not reached
      - Chat.tsx / HomeInput.tsx chip not rendered
      - sandbox: URL not rewritten to /download-file
    """
    page = fresh_page
    _submit_prompt(page, CODEGEN_PROMPT)

    href = _wait_for_chip(page)
    logger.info("Generated chip href: %s", href)

    assert href, "Chip rendered but has empty href"
    assert "sandbox:" not in href, (
        f"Chip href still contains 'sandbox:' — ChatPage.tsx did not rewrite the URL.\n"
        f"  href: {href}"
    )
    assert "/api/v4/chat/download-file" in href, (
        f"Chip does not point to the download endpoint.\n  href: {href}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 2 — Download URL encodes correct query params
# ═══════════════════════════════════════════════════════════════════════════════


def test_download_url_has_file_id_and_filename(fresh_page: Page):
    """
    The chip href must carry file_id and filename as query parameters so
    the backend /download-file endpoint can locate the file.

    Failure modes:
      - download_url built without required params in router.py
      - params lost during SSE→React state pipeline
      - URL double-encoded or truncated
    """
    page = fresh_page
    _submit_prompt(page, CODEGEN_PROMPT)

    href: str = _wait_for_chip(page)
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)

    assert "file_id" in qs, (
        f"'file_id' missing from chip href query string.\n  href: {href}"
    )
    assert "filename" in qs, (
        f"'filename' missing from chip href query string.\n  href: {href}"
    )
    # Sanity: file_id should look like an Azure file ID
    file_id: str = qs["file_id"][0]
    assert file_id.startswith("file") or len(file_id) > 4, (
        f"file_id looks malformed: {file_id!r}"
    )
    # filename must have an extension
    filename: str = qs["filename"][0]
    assert "." in filename, (
        f"filename has no extension — likely a raw ID was used instead: {filename!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 3 — SSE stream emits generated_file event (network-level verification)
# ═══════════════════════════════════════════════════════════════════════════════


def test_sse_stream_emits_generated_file_event(fresh_page: Page):
    """
    Intercepts the /api/v4/chat/stream SSE response and confirms the
    backend emitted at least one 'generated_file' event.

    This is a network-level check — distinct from the DOM chip check.
    It catches cases where the chip failed to render even though the
    server DID emit the event (frontend rendering bug).

    Failure modes:
      - router.py event_stream() never appends to collected_generated_files
      - _sse_event("generated_file", ...) not called
      - agent returned file but annotation parsing failed
    """
    page = fresh_page
    sse_bodies: list[str] = []

    def _capture_sse(resp):
        if "chat/stream" in resp.url and resp.status == 200:
            try:
                sse_bodies.append(resp.text())
            except Exception:
                pass

    page.on("response", _capture_sse)

    try:
        _submit_prompt(page, CODEGEN_PROMPT)
        # Wait for agent to finish
        _wait_for_chip(page)
    finally:
        page.remove_listener("response", _capture_sse)

    assert sse_bodies, (
        "No /api/v4/chat/stream response captured — was the prompt submitted?"
    )

    raw = "\n".join(sse_bodies)
    assert "generated_file" in raw, (
        "SSE stream did not contain a 'generated_file' event.\n"
        "  The agent may have produced a file but the backend did not emit the event.\n"
        "  Check router.py: collected_generated_files accumulation + _sse_event call."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 4 — Download endpoint returns real file bytes
# ═══════════════════════════════════════════════════════════════════════════════


def test_download_endpoint_returns_bytes(fresh_page: Page):
    """
    After the chip appears, clicking it (or fetching the href directly)
    must return non-empty binary content from the real Azure file.

    Failure modes:
      - /download-file endpoint returns 400/500 for the real file_id
      - container_id missing from the URL → 400 from the backend
      - Azure AgentsClient.files.get_content raises on real credentials
      - Bytes returned are empty (file was deleted before download)
    """
    page = fresh_page
    _submit_prompt(page, CODEGEN_PROMPT)

    href: str = _wait_for_chip(page)
    full_url = href if href.startswith("http") else _api(href)

    resp = requests.get(full_url, timeout=60)
    assert resp.status_code == 200, (
        f"Download endpoint returned {resp.status_code}.\n"
        f"  URL: {full_url}\n"
        f"  Body: {resp.text[:300]}"
    )
    content = resp.content
    assert len(content) > 0, f"Download returned 200 but empty body.\n  URL: {full_url}"
    logger.info("Downloaded %d bytes from %s", len(content), full_url)


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 5 — Cosmos persists metadata.generated_files
# ═══════════════════════════════════════════════════════════════════════════════


def test_cosmos_persists_generated_files_metadata(fresh_page: Page):
    """
    After the stream completes, the assistant message in Cosmos must have
    ``metadata.generated_files`` populated.

    This validates the _persist_meta() path in router.py and confirms
    the data needed for session-reload chip restoration is actually written.

    Failure modes:
      - collected_generated_files empty when _persist_meta runs
      - add_message called before SSE loop fills collected_generated_files
      - Cosmos connection error swallowed silently
    """
    page = fresh_page
    _submit_prompt(page, CODEGEN_PROMPT)
    _wait_for_chip(page)

    # Give the backend a moment to finish the Cosmos write
    page.wait_for_timeout(3000)

    session_id = _current_session_id(page)
    messages = _cosmos_messages(session_id)

    assistant_messages = [m for m in messages if m.get("role") == "assistant"]
    assert assistant_messages, (
        f"No assistant messages found in session {session_id}.\n"
        f"  All messages: {[m.get('role') for m in messages]}"
    )

    last_assistant = assistant_messages[-1]
    meta = last_assistant.get("metadata", {})
    gf = meta.get("generated_files", [])

    assert isinstance(gf, list) and len(gf) > 0, (
        f"metadata.generated_files is empty or missing in the last assistant message.\n"
        f"  metadata keys: {list(meta.keys())}\n"
        f"  This means _persist_meta() in router.py did not write the file info."
    )

    first_file = gf[0]
    assert "file_id" in first_file, (
        f"generated_files[0] missing 'file_id' key: {first_file}"
    )
    assert "filename" in first_file, (
        f"generated_files[0] missing 'filename' key: {first_file}"
    )
    assert "download_url" in first_file, (
        f"generated_files[0] missing 'download_url' key: {first_file}"
    )
    assert "sandbox:" not in first_file.get("download_url", ""), (
        f"Cosmos stored a sandbox: URL — backend wrote raw Azure URL without rewriting.\n"
        f"  download_url: {first_file['download_url']}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 6 — Page reload restores chip from Cosmos session history
# ═══════════════════════════════════════════════════════════════════════════════


def test_page_reload_restores_generated_file_chip(fresh_page: Page):
    """
    After the user refreshes the page (browser reload), the generated-file
    chip must reappear by loading session history from Cosmos.

    This validates the ChatService.loadSession() → attachGeneratedFilesToLastMessage
    path in the frontend.

    Failure modes:
      - loadSession() does not parse metadata.generated_files
      - attachGeneratedFilesToLastMessage not called on history load
      - Chips restored but href still contains sandbox: (stored raw in Cosmos)
      - ChatPage / Chat.tsx does not pass generatedFiles to the message component
    """
    page = fresh_page
    _submit_prompt(page, CODEGEN_PROMPT)
    _wait_for_chip(page)
    page.wait_for_timeout(3000)  # let Cosmos write complete

    session_id = _current_session_id(page)
    session_url = CHAT_SESSION_URL_TMPL.format(base=_frontend(), session_id=session_id)

    logger.info("Reloading session at %s", session_url)
    page.goto(session_url, wait_until="domcontentloaded")
    page.wait_for_timeout(6000)  # React needs time to load session data

    chip_selector = "a[href*='download-file']"
    try:
        page.locator(chip_selector).wait_for(state="visible", timeout=20_000)
        chip_visible = True
    except Exception:
        chip_visible = False

    assert chip_visible, (
        f"After page reload, generated-file chip was NOT restored for session "
        f"{session_id}.\n"
        "  Possible causes:\n"
        "    • ChatService.loadSession() ignores metadata.generated_files\n"
        "    • attachGeneratedFilesToLastMessage not called on session load\n"
        "    • Chat.tsx / ChatPage.tsx does not forward generatedFiles to the message"
    )

    href: str = page.locator(chip_selector).first.get_attribute("href") or ""
    assert "sandbox:" not in href, (
        f"Restored chip still has sandbox: href — backend stored raw URL in Cosmos.\n"
        f"  href: {href}"
    )
    assert "/api/v4/chat/download-file" in href, (
        f"Restored chip href does not point to the download endpoint.\n  href: {href}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 7 — Task (plan) flow also generates files correctly
# ═══════════════════════════════════════════════════════════════════════════════


def test_task_flow_generates_file_chip(fresh_page: Page):
    """
    The same file-generation flow must work when the user goes through the
    Plan/Task approval path (not just the direct-chat path).

    Steps:
      1. Send a prompt that routes to a task plan (/plan/<id>).
      2. Approve the plan.
      3. Wait for the plan execution to complete and redirect to /chat/<id>.
      4. Verify the chip appears in the chat output.

    This test is SKIPPED automatically if the prompt routes directly to chat
    (IntentRouter classified it as conversational, not as a task).

    Failure modes:
      - Plan execution does not reach code_interpreter
      - Plan→Chat transition drops the generated_file SSE event
      - Chat page after plan completion does not show chips
    """
    page = fresh_page

    # Use a prompt known to route to a plan
    task_prompt = os.getenv(
        "E2E_TASK_CODEGEN_PROMPT",
        "Create a detailed sales report CSV for Q1 using code interpreter",
    )

    page.locator(TEXTAREA).fill(task_prompt)
    page.wait_for_timeout(300)
    page.locator(SEND_BTN).click()

    # Wait up to 15s for routing decision
    routed_to_plan = False
    for _ in range(15):
        page.wait_for_timeout(1000)
        if "/plan/" in page.url:
            routed_to_plan = True
            break
        if "/chat/" in page.url:
            break  # routed to chat directly — task test is N/A

    if not routed_to_plan:
        pytest.skip(
            "Prompt routed to chat (not plan) — task-specific test not applicable. "
            "Set E2E_TASK_CODEGEN_PROMPT to a prompt that creates a plan."
        )

    logger.info("Routed to plan: %s", page.url)

    # Approve the plan
    approve_btn = page.locator("//button[normalize-space()='Approve Task Plan']")
    try:
        approve_btn.wait_for(state="visible", timeout=30_000)
        approve_btn.click()
    except Exception:
        pytest.skip("Approve Task Plan button not found — plan may need manual setup.")

    # Wait to be redirected back to chat after execution
    deadline = time.time() + AGENT_TIMEOUT_MS / 1000
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        if "/chat/" in page.url:
            break
    else:
        pytest.fail(
            f"Plan execution did not redirect to /chat/ within {AGENT_TIMEOUT_MS} ms.\n"
            f"  Current URL: {page.url}"
        )

    logger.info("Plan completed — now on chat: %s", page.url)

    # Now verify the chip
    href = _wait_for_chip(page, timeout_ms=30_000)
    assert "sandbox:" not in href, (
        f"Task-flow chip href still has sandbox: URL.\n  href: {href}"
    )
    assert "/api/v4/chat/download-file" in href, (
        f"Task-flow chip does not point to download endpoint.\n  href: {href}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TEST 8 — Multiple files: all chips appear, all are downloadable
# ═══════════════════════════════════════════════════════════════════════════════


def test_multiple_files_all_chips_appear_and_download(fresh_page: Page):
    """
    When code_interpreter produces more than one file, ALL chips must appear
    and every download URL must be functional.

    Failure modes:
      - Only the first file is accumulated (off-by-one in event_stream loop)
      - Second file overrides the first in React state
      - One download URL is valid but the other returns 404
    """
    page = fresh_page

    multi_prompt = os.getenv(
        "E2E_MULTI_FILE_PROMPT",
        "Use code interpreter to generate two files: 'data.csv' with 3 rows of sales "
        "data, and 'summary.txt' with a one-line summary.  Save both files.",
    )

    _submit_prompt(page, multi_prompt)

    chip_selector = "a[href*='download-file']"
    deadline = time.time() + AGENT_TIMEOUT_MS / 1000
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        count = page.locator(chip_selector).count()
        if count >= 2:
            logger.info("Found %d chips — checking all downloads", count)
            break
    else:
        count = page.locator(chip_selector).count()
        if count == 0:
            pytest.fail(
                "No chips appeared — agent may not have produced multiple files. "
                "Set E2E_MULTI_FILE_PROMPT to a prompt that reliably produces 2 files."
            )
        elif count == 1:
            pytest.fail(
                "Only 1 chip appeared — second file was lost somewhere in the pipeline.\n"
                "  Check collected_generated_files accumulation in router.py event_stream()."
            )

    # Check every chip is downloadable
    chips = page.locator(chip_selector)
    for i in range(chips.count()):
        href: str = chips.nth(i).get_attribute("href") or ""
        assert href, f"Chip {i} has empty href"
        assert "sandbox:" not in href, f"Chip {i} href has sandbox:: {href}"

        full_url = href if href.startswith("http") else _api(href)
        resp = requests.get(full_url, timeout=60)
        assert resp.status_code == 200, (
            f"Chip {i} download returned {resp.status_code}.\n  URL: {full_url}"
        )
        assert len(resp.content) > 0, (
            f"Chip {i} download returned 200 but empty body.\n  URL: {full_url}"
        )
        logger.info("Chip %d: %d bytes from %s", i, len(resp.content), full_url)
