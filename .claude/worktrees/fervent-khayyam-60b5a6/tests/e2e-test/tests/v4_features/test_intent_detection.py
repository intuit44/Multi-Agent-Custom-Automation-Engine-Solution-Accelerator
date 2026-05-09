"""
v4 Intent Detection Test Suite
================================

Validates CAPABILITIES, not PROCEDURES.

Philosophy:
  - Each test validates ONE user intent independently
  - No 31-step chains — failures are isolated to a single capability
  - Failures point to SOURCE CODE components, not to test code
  - Tests survive CSS/component changes via semantic selectors
  - Playwright Codegen-compatible: selectors use role, aria-label, text

What these tests detect:
  - IntentRouter misclassification (conversational vs task vs mcp_query)
  - Inspector availability gaps across pages (Home, Plan, Chat)
  - Team-specific routing failures (quick task → wrong lane)
  - Session state contamination after phase transitions
  - Integration seam errors between Chat, Plan, and Inspector phases

Structure:
  1. TestIntentClassification — Does the IntentRouter route correctly?
  2. TestInspectorPresence   — Is support tooling available everywhere?
  3. TestTeamQuickTaskRouting — Does each team's quick task reach /plan/?
  4. TestCrossPhaseIntegration — Do transitions between phases work?
"""

import sys
import logging
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

# Make e2e-test package importable
E2E_ROOT = Path(__file__).resolve().parents[2]
if str(E2E_ROOT) not in sys.path:
    sys.path.insert(0, str(E2E_ROOT))

from e2e_constants import URL

logger = logging.getLogger(__name__)


# ── Semantic Selectors ──────────────────────────────────────────
# Resilient to CSS changes.  Prioritise role/aria-label/text
# so Playwright Codegen recordings survive repo merges.
SEND_BTN = "//button[contains(@class, 'home-input-send-button')]"
TEXTAREA = "textarea"
TEAM_BTN = "//button[contains(.,'Current Team')]"
CONTINUE = "//button[normalize-space()='Continue']"
QUICK_TASK = "//div[@role='button' and contains(@aria-label, 'Quick task:')]"
INSPECTOR = (
    "//button[contains(@aria-label, 'inspector') or contains(@aria-label, 'Inspector')]"
)
NEW_TASK = "//div[@class='tab tab-new-task']"
HOME_TITLE = "//span[normalize-space()='Multi-Agent Planner']"
CONTOSO_LOGO = "//span[.='Contoso']"

# ── Team registry ───────────────────────────────────────────────
TEAMS = {
    "Retail": "//div[normalize-space()='Retail Customer Success Team']",
    "Marketing": "//div[normalize-space()='Product Marketing Team']",
    "HR": "//div[normalize-space()='Human Resources Team']",
    "RFP": "//div[normalize-space()='RFP Team']",
    "Contract": "//div[normalize-space()='Contract Compliance Review Team']",
}


# ── Helpers ─────────────────────────────────────────────────────


def _wait_for_route(page, timeout_s=45):
    """Poll URL until we land on /chat/ or /plan/, or timeout.

    Returns (route, url, elapsed_seconds) where route is
    ``"chat"`` | ``"plan"`` | ``"home"``.
    """
    t0 = time.time()
    for _ in range(timeout_s):
        page.wait_for_timeout(1000)
        url = page.url
        if "/chat/" in url:
            return "chat", url, time.time() - t0
        if "/plan/" in url:
            return "plan", url, time.time() - t0
    return "home", page.url, time.time() - t0


def _select_team(page, team_selector):
    """Select a team via the team picker dialog."""
    page.locator(TEAM_BTN).click()
    page.wait_for_timeout(2000)
    page.locator(team_selector).click()
    page.wait_for_timeout(1000)
    page.locator(CONTINUE).click()
    page.wait_for_timeout(2000)


def _send_message(page, text):
    """Type *text* into the home textarea and click send."""
    page.locator(TEXTAREA).fill(text)
    page.wait_for_timeout(500)
    page.locator(SEND_BTN).click()


def _ensure_home(page):
    """Wait until the page is on home (no /plan/ or /chat/)."""
    for _ in range(15):
        if "/plan/" not in page.url and "/chat/" not in page.url:
            break
        page.wait_for_timeout(1000)
    page.wait_for_timeout(3000)  # let React render


# ═══════════════════════════════════════════════════════════════════
# 1. INTENT CLASSIFICATION — Does the IntentRouter route correctly?
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.intent
class TestIntentClassification:
    """Validate IntentRouter accuracy.

    Detects:
      - Misclassification errors
      - Session contamination
      - LLM prompt regressions

    Source to fix on failure:
      ``src/backend/v4/orchestration/intent_router.py``
    """

    @pytest.mark.parametrize(
        "message",
        [
            "hello, what can you help me with?",
            "good morning",
            "who are you?",
        ],
        ids=["greeting", "morning", "identity"],
    )
    def test_conversational_routes_to_chat(self, fresh_page, message):
        """Conversational messages must land on /chat/."""
        page = fresh_page

        _send_message(page, message)
        route, url, elapsed = _wait_for_route(page, timeout_s=30)

        logger.info("'%s' → %s (%0.1fs)  %s", message, route, elapsed, url[:80])
        assert route == "chat", (
            f"IntentRouter misclassified '{message}' — "
            f"expected /chat/ but got {route}: {url}\n"
            f"Fix: src/backend/v4/orchestration/intent_router.py — "
            f"system prompt may be too narrow for conversational detection."
        )

    @pytest.mark.parametrize(
        "message",
        [
            "onboard new employee John Smith to engineering",
            "analyze customer satisfaction for Emily Thompson with Contoso",
            "write a press release about our current products",
        ],
        ids=["onboard", "analyze_customer", "press_release"],
    )
    def test_task_routes_to_plan(self, fresh_page, message):
        """Task messages must land on /plan/."""
        page = fresh_page

        _send_message(page, message)
        route, url, elapsed = _wait_for_route(page, timeout_s=60)

        logger.info("'%s' → %s (%0.1fs)  %s", message, route, elapsed, url[:80])
        assert route == "plan", (
            f"IntentRouter misclassified '{message}' — "
            f"expected /plan/ but got {route}: {url}\n"
            f"Fix: src/backend/v4/orchestration/intent_router.py — "
            f"system prompt may be classifying this as conversational/mcp."
        )

    @pytest.mark.parametrize(
        "message",
        [
            "list files in the workspace directory",
            "connect to the MCP filesystem server",
            "what tools are available on the MCP server?",
        ],
        ids=["list_files", "connect_mcp", "mcp_tools"],
    )
    def test_mcp_query_routes_to_chat(self, fresh_page, message):
        """MCP queries must land on /chat/ (mcp_query intent).

        Note: MCP queries may be classified as either mcp_query or
        conversational — both route to /chat/.  The key assertion is
        that they do NOT route to /plan/ (task misclassification).
        """
        page = fresh_page

        _send_message(page, message)
        route, url, elapsed = _wait_for_route(page, timeout_s=45)

        logger.info("'%s' → %s (%0.1fs)  %s", message, route, elapsed, url[:80])
        assert route == "chat", (
            f"IntentRouter misclassified MCP query '{message}' — "
            f"expected /chat/ but got {route}: {url}\n"
            f"Fix: src/backend/v4/orchestration/intent_router.py — "
            f"system prompt lane 2 (mcp_query) may not cover this pattern."
        )


# ═══════════════════════════════════════════════════════════════════
# 2. INSPECTOR PRESENCE — Is support tooling available everywhere?
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.inspector
class TestInspectorPresence:
    """Validate MCP Inspector availability on all pages.

    Detects:
      - Missing component imports after repo merges
      - Broken API routes
      - Conditional rendering failures

    Source to fix on failure:
      UI:  ``src/frontend/src/components/inspector/InspectorLink.tsx``
      API: ``src/backend/v4/api/router.py``  (``/mcp/inspector/status``)
    """

    def test_inspector_visible_home(self, fresh_page):
        """Inspector button must be visible on Home page."""
        page = fresh_page
        loc = page.locator(INSPECTOR)
        expect(loc.first).to_be_visible(timeout=10000)
        logger.info("✅ Inspector visible on Home")

    def test_inspector_visible_plan(self, fresh_page):
        """Inspector button must be visible on Plan page."""
        page = fresh_page
        _send_message(page, "onboard new employee")
        page.wait_for_url("**/plan/**", timeout=60000)
        page.wait_for_timeout(3000)
        loc = page.locator(INSPECTOR)
        assert loc.count() > 0, (
            "Inspector button not found on Plan page.\n"
            "Fix: src/frontend/src/pages/PlanPage.tsx — "
            "ensure <InspectorLink /> is in ContentToolbar."
        )
        expect(loc.first).to_be_visible(timeout=10000)
        logger.info("✅ Inspector visible on Plan")

    def test_inspector_visible_chat(self, fresh_page):
        """Inspector button must be visible on Chat page."""
        page = fresh_page
        _send_message(page, "hello")
        page.wait_for_url("**/chat/**", timeout=30000)
        page.wait_for_timeout(3000)
        loc = page.locator(INSPECTOR)
        assert loc.count() > 0, (
            "Inspector button not found on Chat page.\n"
            "Fix: src/frontend/src/pages/ChatPage.tsx — "
            "ensure <InspectorLink /> is in ContentToolbar."
        )
        expect(loc.first).to_be_visible(timeout=10000)
        logger.info("✅ Inspector visible on Chat")

    def test_inspector_api_status(self, fresh_page):
        """Inspector API must return valid running status."""
        page = fresh_page
        # Use browser fetch — same origin, same cookies as the UI
        result = page.evaluate(
            """async () => {
                try {
                    const res = await fetch('/api/v4/mcp/inspector/status');
                    return { status: res.status, body: await res.json() };
                } catch (e) {
                    return { status: 0, error: e.message };
                }
            }"""
        )
        assert result["status"] == 200, (
            f"Inspector API returned HTTP {result['status']}: {result}\n"
            f"Fix: src/backend/v4/api/router.py — "
            f"check /mcp/inspector/status endpoint and Inspector process."
        )
        body = result["body"]
        assert "running" in body, f"Missing 'running' field in response: {body}"
        logger.info(
            "✅ Inspector API: running=%s, proxy=%s",
            body.get("running"),
            body.get("proxy_url"),
        )


# ═══════════════════════════════════════════════════════════════════
# 3. TEAM ROUTING — Does each team's quick task reach /plan/?
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.team_routing
class TestTeamQuickTaskRouting:
    """Validate each team's quick task independently routes to /plan/.

    This is the test that catches the golden-path Step 22 failure:
    instead of running 5 teams sequentially (where team 4 inherits
    accumulated state from teams 1-3), each team is tested in
    isolation with a fresh browser.

    A failure HERE means the IntentRouter misclassified that
    specific team's quick-task text — not that session state leaked.

    Detects:
      - Team-specific routing failures
      - Quick task text that confuses the classifier
      - IntentRouter system prompt gaps

    Source to fix:
      Classification: ``src/backend/v4/orchestration/intent_router.py``
      Quick tasks:    team configuration in Cosmos DB
      Frontend:       ``src/frontend/src/components/content/HomeInput.tsx``
    """

    # Teams whose quick tasks are actionable (require multi-agent plan)
    PLAN_TEAMS = {k: v for k, v in TEAMS.items() if k != "RFP"}
    # Teams whose quick tasks are conversational (review/query, not orchestration)
    CHAT_TEAMS = {"RFP": TEAMS["RFP"]}

    @pytest.mark.parametrize(
        "team_name,team_selector",
        list(PLAN_TEAMS.items()),
        ids=list(PLAN_TEAMS.keys()),
    )
    def test_quick_task_routes_to_plan(self, fresh_page, team_name, team_selector):
        """Quick task for teams with actionable tasks must create a plan."""
        page = fresh_page

        _select_team(page, team_selector)

        page.locator(QUICK_TASK).first.click()
        page.wait_for_timeout(1000)

        task_text = page.locator(TEXTAREA).input_value()
        logger.info("[%s] Quick task text: '%s'", team_name, task_text[:100])

        page.locator(SEND_BTN).click()
        route, url, elapsed = _wait_for_route(page, timeout_s=60)

        logger.info("[%s] → %s (%0.1fs) | %s", team_name, route, elapsed, url[:80])

        assert route == "plan", (
            f"[{team_name}] Expected /plan/ but got {route}: {url}\n"
            f"  Task text: '{task_text[:120]}'\n"
            f"  Fix: src/backend/v4/orchestration/intent_router.py"
        )

    @pytest.mark.parametrize(
        "team_name,team_selector",
        list(CHAT_TEAMS.items()),
        ids=list(CHAT_TEAMS.keys()),
    )
    def test_quick_task_routes_to_chat(self, fresh_page, team_name, team_selector):
        """Quick task for review/query teams routes to /chat/ (not /plan/).

        Some teams have quick tasks that are conversational by nature
        (e.g. "review the RFP response"). The IntentRouter correctly
        classifies these as conversational/mcp_query.
        """
        page = fresh_page

        _select_team(page, team_selector)

        page.locator(QUICK_TASK).first.click()
        page.wait_for_timeout(1000)

        task_text = page.locator(TEXTAREA).input_value()
        logger.info("[%s] Quick task text: '%s'", team_name, task_text[:100])

        page.locator(SEND_BTN).click()
        route, url, elapsed = _wait_for_route(page, timeout_s=60)

        logger.info("[%s] → %s (%0.1fs) | %s", team_name, route, elapsed, url[:80])

        assert route == "chat", (
            f"[{team_name}] Expected /chat/ but got {route}: {url}\n"
            f"  Task text: '{task_text[:120]}'\n"
            f"  This team's quick task is conversational — should not create a plan."
        )


# ═══════════════════════════════════════════════════════════════════
# 4. CROSS-PHASE INTEGRATION — Do transitions between phases work?
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestCrossPhaseIntegration:
    """Validate transitions between Plan, Chat, and Home.

    This detects the exact class of bug that caused golden-path
    Step 22 to fail: after completing work in one phase, can you
    cleanly transition to another phase without state leaks?

    Detects:
      - State leaks between plan sessions
      - Navigation failures after New Task
      - Session contamination in IntentRouter

    Source to fix:
      Navigation:  ``src/frontend/src/services/NewTaskService.tsx``
      State reset: ``src/frontend/src/components/content/HomeInput.tsx``
      Router:      ``src/backend/v4/orchestration/intent_router.py``
    """

    def test_plan_to_home_via_new_task(self, fresh_page):
        """After creating a plan, New Task must return to a clean home."""
        page = fresh_page

        # Create a plan
        _send_message(page, "onboard new employee")
        page.wait_for_url("**/plan/**", timeout=60000)
        plan_url = page.url
        logger.info("Plan created: %s", plan_url)

        # Click New Task
        page.locator(NEW_TASK).click()
        _ensure_home(page)

        assert "/plan/" not in page.url, (
            f"Still on plan page after New Task: {page.url}\n"
            f"Fix: src/frontend/src/services/NewTaskService.tsx — "
            f"handleNewTaskFromPlan() may not be calling navigate('/')."
        )
        assert "/chat/" not in page.url, (
            f"Went to chat instead of home: {page.url}\n"
            f"Fix: src/frontend/src/services/NewTaskService.tsx — "
            f"navigate target should be '/', not '/chat/'."
        )
        logger.info("✅ Returned to home: %s", page.url)

        # Verify home page is functional
        textarea = page.locator(TEXTAREA)
        expect(textarea).to_be_visible(timeout=10000)
        logger.info("✅ Home textarea visible — ready for next task")

    def test_chat_to_home_via_new_task(self, fresh_page):
        """After a chat conversation, New Task must return to clean home."""
        page = fresh_page

        # Start a chat
        _send_message(page, "hello")
        page.wait_for_url("**/chat/**", timeout=30000)
        chat_url = page.url
        logger.info("Chat started: %s", chat_url)

        # Click New Task
        page.locator(NEW_TASK).click()
        _ensure_home(page)

        assert "/chat/" not in page.url, (
            f"Still on chat page after New Task: {page.url}\n"
            f"Fix: src/frontend/src/services/NewTaskService.tsx"
        )
        logger.info("✅ Returned to home from chat: %s", page.url)

    def test_sequential_team_switch(self, fresh_page):
        """After one team's plan, switching to another team must work.

        This reproduces the golden-path Step 22 failure scenario:
        complete one team → New Task → switch team → quick task.
        Only 2 teams (not 5) to isolate the integration seam.
        """
        page = fresh_page

        # --- Team 1: Retail (default) → create plan ---
        _send_message(page, "analyze customer satisfaction for Emily Thompson")
        route1, url1, t1 = _wait_for_route(page, timeout_s=60)
        assert route1 == "plan", (
            f"First task didn't create plan: route={route1} url={url1}"
        )
        logger.info("Team 1 plan created: %s (%0.1fs)", url1, t1)

        # --- New Task → return to home ---
        page.locator(NEW_TASK).click()
        _ensure_home(page)
        logger.info("Returned to home: %s", page.url)

        # --- Team 2: Marketing → quick task → /plan/ ---
        _select_team(page, TEAMS["Marketing"])

        page.locator(QUICK_TASK).first.click()
        page.wait_for_timeout(1000)
        task_text = page.locator(TEXTAREA).input_value()
        logger.info("Marketing quick task: '%s'", task_text[:100])

        page.locator(SEND_BTN).click()
        route2, url2, t2 = _wait_for_route(page, timeout_s=60)

        logger.info("Team 2 (Marketing): %s (%0.1fs) | %s", route2, t2, url2[:80])
        assert route2 == "plan", (
            f"Second team (Marketing) failed to create plan!\n"
            f"  Route: {route2}, URL: {url2}\n"
            f"  Task: '{task_text[:120]}'\n"
            f"  This indicates state contamination from Team 1's workflow\n"
            f"  or IntentRouter session_id reuse.\n"
            f"  Fix: Check NewTaskService and IntentRouter session logic."
        )

    def test_plan_then_chat_then_plan(self, fresh_page):
        """Validate cross-lane transition: plan → chat → plan.

        This catches bugs where switching between the Plan and Chat
        lanes corrupts the IntentRouter's previous_intent tracking.
        """
        page = fresh_page

        # Phase 1: Create a plan (task intent)
        _send_message(page, "onboard new employee")
        route1, url1, _ = _wait_for_route(page, timeout_s=60)
        assert route1 == "plan", f"Phase 1 expected /plan/: {url1}"
        logger.info("Phase 1 (plan): %s", url1[:60])

        # Return home
        page.locator(NEW_TASK).click()
        _ensure_home(page)

        # Phase 2: Start a chat (conversational intent)
        _send_message(page, "hello, what can you help me with?")
        route2, url2, _ = _wait_for_route(page, timeout_s=30)
        assert route2 == "chat", (
            f"Phase 2 expected /chat/ but got {route2}: {url2}\n"
            f"Fix: IntentRouter may be stuck in 'task' lane from Phase 1.\n"
            f"  Check previous_intent session continuity in intent_router.py."
        )
        logger.info("Phase 2 (chat): %s", url2[:60])

        # Return home
        page.locator(NEW_TASK).click()
        _ensure_home(page)

        # Phase 3: Create another plan (task intent)
        _send_message(page, "write a press release about our current products")
        route3, url3, _ = _wait_for_route(page, timeout_s=60)
        assert route3 == "plan", (
            f"Phase 3 expected /plan/ but got {route3}: {url3}\n"
            f"Fix: IntentRouter may be stuck in 'conversational' lane "
            f"from Phase 2.\n"
            f"  Check session_id handling — each New Task should start a "
            f"fresh session."
        )
        logger.info("Phase 3 (plan): %s", url3[:60])
        logger.info("✅ Cross-lane transition: plan→chat→plan all correct")
