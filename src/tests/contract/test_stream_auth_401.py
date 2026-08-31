"""Regression: POST /api/v4/chat/message/stream must answer 401 (not an
unhandled 500) when auth fails.

The frontend SSE client (apiClient.stream) only refresh-retries on 401; a 500
skips that retry, kills the chat, and forces a full page reload. The endpoint
used to call get_authenticated_user_details() directly, so PermissionError
propagated unhandled -> 500. It now mirrors workspace_router's try/except and
raises HTTPException(401).
"""
from fastapi.testclient import TestClient

import v4.api.router as router


def test_chat_stream_auth_failure_is_401_not_500(app, monkeypatch):
    def _deny(*_args, **_kwargs):
        raise PermissionError(
            "Authentication required. No EasyAuth principal found in headers."
        )

    monkeypatch.setattr(router, "get_authenticated_user_details", _deny)

    client = TestClient(app)
    resp = client.post("/api/v4/chat/message/stream", json={"message": "ping"})

    assert resp.status_code == 401, (
        f"expected 401 so the frontend retry engages, got {resp.status_code}: "
        f"{resp.text[:300]}"
    )
