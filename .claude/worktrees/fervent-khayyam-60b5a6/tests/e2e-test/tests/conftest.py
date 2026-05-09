"""
Pytest configuration and fixtures for KM Generic Golden Path tests
"""

import os
import io
import logging
import atexit
import subprocess
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup


# ── Virtual display helper ───────────────────────────────────────────
_xvfb_proc = None  # module-level so atexit can clean up


def _has_real_display() -> bool:
    """Return True if DISPLAY points to a real X server (not 'dummy')."""
    display = os.environ.get("DISPLAY", "")
    return bool(display) and display != "dummy" and display.startswith(":")


def _ensure_virtual_display() -> bool:
    """Start Xvfb on :99 if no real display is available.

    Returns True if a usable DISPLAY is now set (real or virtual).
    """
    global _xvfb_proc
    if _has_real_display():
        return True

    if _xvfb_proc is not None:  # already started by us
        return True

    if not shutil.which("Xvfb"):
        logging.warning("Xvfb not found — falling back to headless mode")
        return False

    try:
        import time

        _xvfb_proc = subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = ":99"
        # Xvfb needs a moment to bind the display socket before clients connect
        time.sleep(1)
        # Verify it's still running (didn't crash on startup, e.g. :99 in use)
        if _xvfb_proc.poll() is not None:
            logging.warning("Xvfb exited immediately (rc=%s)", _xvfb_proc.returncode)
            _xvfb_proc = None
            return False
        logging.info("Started Xvfb on :99 (pid %s)", _xvfb_proc.pid)
        return True
    except OSError as exc:
        logging.warning("Failed to start Xvfb: %s — falling back to headless", exc)
        return False


def _stop_virtual_display():
    global _xvfb_proc
    if _xvfb_proc is not None:
        _xvfb_proc.terminate()
        _xvfb_proc.wait(timeout=5)
        _xvfb_proc = None


atexit.register(_stop_virtual_display)

# Make the e2e-test package importable when pytest is launched from repo root.
E2E_ROOT = Path(__file__).resolve().parents[1]
if str(E2E_ROOT) not in sys.path:
    sys.path.insert(0, str(E2E_ROOT))

# Create screenshots directory if it doesn't exist
SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


@pytest.fixture
def subtests(request):
    """Fixture to enable subtests for step-by-step reporting in HTML"""

    class SubTests:
        """SubTests class for managing subtest contexts"""

        def __init__(self, request):
            self.request = request
            self._current_subtest = None

        def test(self, msg=None):
            """Create a new subtest context"""
            return SubTestContext(self, msg)

    class SubTestContext:
        """Context manager for individual subtests"""

        def __init__(self, parent, msg):
            self.parent = parent
            self.msg = msg
            self.logger = logging.getLogger()
            self.stream = None
            self.handler = None

        def __enter__(self):
            # Create a dedicated log stream for this subtest
            self.stream = io.StringIO()
            self.handler = logging.StreamHandler(self.stream)
            self.handler.setLevel(logging.INFO)
            self.logger.addHandler(self.handler)
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            # Flush logs
            if self.handler:
                self.handler.flush()
                log_output = self.stream.getvalue()
                self.logger.removeHandler(self.handler)

                # Create a report entry for this subtest
                if hasattr(self.parent.request.node, "user_properties"):
                    self.parent.request.node.user_properties.append(
                        (
                            "subtest",
                            {
                                "msg": self.msg,
                                "logs": log_output,
                                "passed": exc_type is None,
                            },
                        )
                    )

            # Don't suppress exceptions - let them propagate
            return False

    return SubTests(request)


@pytest.fixture(scope="session")
def login_logout():
    """Perform login and browser close once in a session.

    Display strategy (no code changes needed between local / CI / container):
      HEADED=0 (default) → headless, works everywhere.
      HEADED=1           → tries real DISPLAY, falls back to Xvfb, then headless.
    """
    # ── Resolve display BEFORE sync_playwright() spawns its Node server,
    #    because the child process inherits DISPLAY at fork time.
    headed = os.environ.get("HEADED", "0") == "1"
    if headed:
        if not _ensure_virtual_display():
            logging.warning(
                "HEADED=1 requested but no display available — running headless"
            )
            headed = False

    with sync_playwright() as playwright_instance:
        browser = playwright_instance.chromium.launch(
            headless=not headed,
            args=["--start-maximized", "--no-sandbox"] if headed else ["--no-sandbox"],
        )
        context = browser.new_context(
            no_viewport=True if headed else False,
            viewport={"width": 1280, "height": 720} if not headed else None,
        )
        context.set_default_timeout(150000)
        page = context.new_page()
        # Navigate to the app URL (import here to avoid stale .pyc cache)
        from e2e_constants import URL

        logging.info("Fixture navigating to: %s", URL)
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(8000)
        logging.info("Fixture page URL after load: %s", page.url)

        yield page
        # Perform close the browser
        browser.close()


log_streams = {}


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Prepare StringIO for capturing logs"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)

    logger = logging.getLogger()
    logger.addHandler(handler)

    # Save handler and stream
    log_streams[item.nodeid] = (handler, stream)


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_html_report_title(report):
    """Set custom HTML report title"""
    report.title = "MACAE-v3_test_Automation_Report"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Generate test report with logs, subtest details, and screenshots on failure"""
    outcome = yield
    report = outcome.get_result()

    # Capture screenshot on failure
    if report.when == "call" and report.failed:
        # Get the page fixture if it exists
        if "login_logout" in item.fixturenames:
            page = item.funcargs.get("login_logout")
            if page:
                try:
                    # Generate screenshot filename with timestamp
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    test_name = item.name.replace(" ", "_").replace("/", "_")
                    screenshot_name = f"screenshot_{test_name}_{timestamp}.png"
                    screenshot_path = os.path.join(SCREENSHOTS_DIR, screenshot_name)

                    # Take screenshot
                    page.screenshot(path=screenshot_path)

                    # Add screenshot link to report
                    if not hasattr(report, "extra"):
                        report.extra = []

                    # Add screenshot as a link in the Links column
                    # Use relative path from report.html location
                    relative_path = os.path.relpath(
                        screenshot_path, os.path.dirname(os.path.abspath("report.html"))
                    )

                    # pytest-html expects this format for extras
                    from pytest_html import extras

                    report.extra.append(extras.url(relative_path, name="Screenshot"))

                    logging.info("Screenshot saved: %s", screenshot_path)
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logging.error("Failed to capture screenshot: %s", str(exc))

    handler, stream = log_streams.get(item.nodeid, (None, None))

    if handler and stream:
        # Make sure logs are flushed
        handler.flush()
        log_output = stream.getvalue()

        # Only remove the handler, don't close the stream yet
        logger = logging.getLogger()
        logger.removeHandler(handler)

        # Check if there are subtests
        subtests_html = ""
        if hasattr(item, "user_properties"):
            item_subtests = [
                prop[1] for prop in item.user_properties if prop[0] == "subtest"
            ]
            if item_subtests:
                subtests_html = (
                    "<div style='margin-top: 10px;'>"
                    "<strong>Step-by-Step Details:</strong>"
                    "<ul style='list-style: none; padding-left: 0;'>"
                )
                for idx, subtest in enumerate(item_subtests, 1):
                    status = "✅ PASSED" if subtest.get("passed") else "❌ FAILED"
                    status_color = "green" if subtest.get("passed") else "red"
                    subtests_html += (
                        f"<li style='margin: 10px 0; padding: 10px; "
                        f"border-left: 3px solid {status_color}; "
                        f"background-color: #f9f9f9;'>"
                    )
                    subtests_html += (
                        f"<div style='font-weight: bold; color: {status_color};'>"
                        f"{status} - {subtest.get('msg', f'Step {idx}')}</div>"
                    )
                    if subtest.get("logs"):
                        subtests_html += (
                            f"<pre style='margin: 5px 0; padding: 5px; "
                            f"background-color: #fff; border: 1px solid #ddd; "
                            f"font-size: 11px;'>{subtest.get('logs').strip()}</pre>"
                        )
                    subtests_html += "</li>"
                subtests_html += "</ul></div>"

        # Combine main log output with subtests
        if subtests_html:
            report.description = f"<pre>{log_output.strip()}</pre>{subtests_html}"
        else:
            report.description = f"<pre>{log_output.strip()}</pre>"

        # Clean up references
        log_streams.pop(item.nodeid, None)
    else:
        report.description = ""


def pytest_collection_modifyitems(items):
    """Modify test items to use custom node IDs"""
    for item in items:
        if hasattr(item, "callspec"):
            # Check for 'description' parameter first (for Golden Path tests)
            description = item.callspec.params.get("description")
            if description:
                # pylint: disable=protected-access
                item._nodeid = f"Golden Path - KM Generic - {description}"
            # Fallback to 'prompt' parameter for other tests
            else:
                prompt = item.callspec.params.get("prompt")
                if prompt:
                    # This controls how the test name appears in the report
                    # pylint: disable=protected-access
                    item._nodeid = prompt


def rename_duration_column():
    """Rename Duration column to Execution Time in HTML report"""
    report_path = os.path.abspath("report.html")
    if not os.path.exists(report_path):
        print("Report file not found, skipping column rename.")
        return

    with open(report_path, "r", encoding="utf-8") as report_file:
        soup = BeautifulSoup(report_file, "html.parser")

    # Find and rename the header
    headers = soup.select("table#results-table thead th")
    for header_th in headers:
        if header_th.text.strip() == "Duration":
            header_th.string = "Execution Time"
            break
    else:
        print("'Duration' column not found in report.")

    with open(report_path, "w", encoding="utf-8") as report_file:
        report_file.write(str(soup))


# Register this function to run after everything is done
atexit.register(rename_duration_column)
