"""Tests for the v4 FastAPI application (src/backend/app.py).

These tests validate the real endpoints exposed by the v4 application:
  - GET  /config                         (app.py)
  - POST /api/user_browser_language      (app.py)
  - POST /api/v4/process_request         (v4/api/router.py)
  - GET  /api/v4/init_team               (v4/api/router.py)

NOTE: Do **NOT** use ``sys.modules[...] = MagicMock()`` at module level.
      That poisons the Python module cache for the entire pytest process and
      breaks unrelated tests (fastmcp, agent_framework, pydantic, etc.).
      Use ``unittest.mock.patch()`` on specific callables instead.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Environment defaults required by app_config at import time
# ---------------------------------------------------------------------------
os.environ.setdefault("COSMOSDB_ENDPOINT", "https://mock-endpoint")
os.environ.setdefault("COSMOSDB_KEY", "mock-key")
os.environ.setdefault("COSMOSDB_DATABASE", "mock-database")
os.environ.setdefault("COSMOSDB_CONTAINER", "mock-container")
os.environ.setdefault(
    "APPLICATIONINSIGHTS_CONNECTION_STRING",
    "InstrumentationKey=mock-key;IngestionEndpoint=https://mock-ingestion",
)
os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT_NAME", "mock-deployment-name")
os.environ.setdefault("AZURE_OPENAI_API_VERSION", "2023-01-01")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://mock-openai-endpoint")
os.environ.setdefault(
    "AZURE_AI_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000"
)
os.environ.setdefault("AZURE_AI_RESOURCE_GROUP", "rg-test")
os.environ.setdefault("AZURE_AI_PROJECT_NAME", "proj-test")
os.environ.setdefault("AZURE_AI_AGENT_ENDPOINT", "https://agents.example.com/")
os.environ.setdefault("USER_LOCAL_BROWSER_LANGUAGE", "en-US")

# ---------------------------------------------------------------------------
# Import the FastAPI app — patch only the telemetry initializer
# ---------------------------------------------------------------------------
with patch("azure.monitor.opentelemetry.configure_azure_monitor", MagicMock()):
    from app import app  # noqa: E402

client = TestClient(app)

# Real v4 endpoint paths
PROCESS_REQUEST_PATH = "/api/v4/process_request"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _mock_auth(monkeypatch):
    """Ensure every request is treated as authenticated."""
    monkeypatch.setattr(
        "auth.auth_utils.get_authenticated_user_details",
        lambda headers: {"user_principal_id": "mock-user-id"},
    )


# ---------------------------------------------------------------------------
# Tests — app.py direct endpoints
# ---------------------------------------------------------------------------
class TestAppDirectEndpoints:
    """Tests for endpoints defined directly in app.py."""

    def test_root_returns_404(self):
        """GET / is not defined — expect 404."""
        response = client.get("/")
        assert response.status_code == 404

    def test_get_frontend_config(self):
        """GET /config returns runtime frontend configuration."""
        response = client.get("/config")
        assert response.status_code == 200
        data = response.json()
        assert "API_URL" in data
        assert "ENABLE_AUTH" in data

    def test_user_browser_language(self):
        """POST /api/user_browser_language accepts a language string."""
        response = client.post(
            "/api/user_browser_language",
            json={"language": "es-CO"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "Language received successfully"

    def test_user_browser_language_missing_field(self):
        """POST /api/user_browser_language with missing field returns 422."""
        response = client.post("/api/user_browser_language", json={})
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Tests — /api/v4/process_request
# ---------------------------------------------------------------------------
class TestProcessRequest:
    """Tests for POST /api/v4/process_request (v4 router)."""

    def test_invalid_json_body(self):
        """Malformed JSON body returns 422 (FastAPI validation)."""
        response = client.post(
            PROCESS_REQUEST_PATH,
            content="{invalid: json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422

    def test_missing_description_field(self):
        """Omitting required 'description' field returns 422."""
        response = client.post(
            PROCESS_REQUEST_PATH,
            json={"session_id": "s1"},
        )
        assert response.status_code == 422
        assert "detail" in response.json()

    def test_missing_session_id_field(self):
        """Omitting required 'session_id' field returns 422."""
        response = client.post(
            PROCESS_REQUEST_PATH,
            json={"description": "Onboard new employee"},
        )
        assert response.status_code == 422

    @patch("v4.api.router.OrchestrationManager")
    @patch("v4.api.router.TeamService")
    @patch("v4.api.router.track_event_if_configured")
    @patch("v4.api.router.DatabaseFactory")
    @patch("v4.api.router.rai_success", new_callable=AsyncMock, return_value=True)
    def test_success_creates_plan(
        self, mock_rai, mock_db_factory, mock_track, mock_team_svc, mock_orch
    ):
        """Valid request with passing RAI creates a plan and returns 200."""
        # Setup mock chain: user has a current team
        mock_store = AsyncMock()
        mock_current_team = MagicMock()
        mock_current_team.team_id = "team-hr"
        mock_store.get_current_team.return_value = mock_current_team
        mock_store.get_team_by_id.return_value = MagicMock()  # team config
        mock_store.add_plan = AsyncMock()
        mock_db_factory.get_database = AsyncMock(return_value=mock_store)
        mock_orch.get_current_or_new_orchestration = AsyncMock()
        # The background task calls OrchestrationManager().run_orchestration(...)
        mock_orch_instance = AsyncMock()
        mock_orch.return_value = mock_orch_instance

        response = client.post(
            PROCESS_REQUEST_PATH,
            json={
                "session_id": "test-session-123",
                "description": "Onboard new employee",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "plan_id" in data
        assert "session_id" in data
        assert data["status"] == "Request started successfully"
        assert data["session_id"] == "test-session-123"
        mock_store.add_plan.assert_called_once()

    @patch("v4.api.router.track_event_if_configured")
    @patch("v4.api.router.DatabaseFactory")
    @patch("v4.api.router.rai_success", new_callable=AsyncMock, return_value=False)
    def test_rai_failure_returns_400(self, mock_rai, mock_db_factory, mock_track):
        """When RAI check fails, endpoint returns 400 with safety message."""
        mock_store = AsyncMock()
        mock_current_team = MagicMock()
        mock_current_team.team_id = "team-hr"
        mock_store.get_current_team.return_value = mock_current_team
        mock_store.get_team_by_id.return_value = MagicMock()
        mock_db_factory.get_database = AsyncMock(return_value=mock_store)

        response = client.post(
            PROCESS_REQUEST_PATH,
            json={
                "session_id": "test-session-456",
                "description": "harmful content test",
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "safety" in data["detail"].lower()

    @patch("v4.api.router.DatabaseFactory")
    def test_no_team_configured_returns_404(self, mock_db_factory):
        """When user has no current team, endpoint returns 404."""
        mock_store = AsyncMock()
        mock_store.get_current_team.return_value = None
        mock_db_factory.get_database = AsyncMock(return_value=mock_store)

        response = client.post(
            PROCESS_REQUEST_PATH,
            json={
                "session_id": "test-session-789",
                "description": "Onboard new employee",
            },
        )

        assert response.status_code == 404
        assert "team" in response.json()["detail"].lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
