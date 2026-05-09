"""
v4_features fixture overrides.

The parent conftest provides ``login_logout`` with scope="session"
(shared browser, shared tab).  That's correct for the golden-path
smoke test, but intent-detection tests need each test to start from
a CLEAN home page so that routing results are not contaminated by
the previous test's final URL.

This module adds a wrapper fixture ``fresh_page`` that:
  1. Reuses the same browser tab (fast — no new browser per test)
  2. Navigates back to home (``/``) before each test
  3. Waits for React to mount the HomeInput component
"""

import logging

import pytest

logger = logging.getLogger(__name__)


@pytest.fixture()
def fresh_page(login_logout):
    """Yield a page guaranteed to be on the Home route (``/``).

    Uses the session-scoped ``login_logout`` fixture for speed,
    but navigates back to home and waits for the SPA to be ready
    before yielding.
    """
    page = login_logout
    current = page.url

    # If not already on home, navigate there
    if "/plan/" in current or "/chat/" in current:
        logger.info("fresh_page: returning to home from %s", current)
        page.goto("http://localhost:3001", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
    elif current.rstrip("/") != "http://localhost:3001":
        logger.info("fresh_page: navigating to home (was: %s)", current)
        page.goto("http://localhost:3001", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
    else:
        # Already on home — just wait for any pending renders
        page.wait_for_timeout(2000)

    # Verify the home page is ready
    try:
        page.locator("textarea").wait_for(state="visible", timeout=10000)
    except Exception:
        logger.warning("fresh_page: textarea not visible, reloading")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

    logger.info("fresh_page: ready at %s", page.url)
    yield page
