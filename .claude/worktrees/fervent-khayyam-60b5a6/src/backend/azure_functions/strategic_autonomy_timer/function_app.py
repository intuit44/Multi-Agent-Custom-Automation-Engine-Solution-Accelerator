"""Azure Function Timer trigger for strategic autonomy control loop.

This runs outside the accelerator runtime so autonomy is event/schedule driven,
not a Python while-loop in the API container.
"""

import logging
import os
from typing import Any, Dict

import azure.functions as func
import httpx

app = func.FunctionApp()


def _build_url() -> str:
    base = os.getenv("ACCELERATOR_API_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise ValueError("Missing ACCELERATOR_API_BASE_URL")
    return f"{base}/api/strategic/analyze"


def _headers() -> Dict[str, str]:
    token = os.getenv("ACCELERATOR_API_BEARER_TOKEN", "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


@app.timer_trigger(
    arg_name="timer",
    schedule="%STRATEGIC_TIMER_SCHEDULE%",
    run_on_startup=False,
    use_monitor=True,
)
async def strategic_autonomy_timer(timer: func.TimerRequest) -> None:
    """Invoke strategic analysis endpoint on schedule."""
    try:
        url = _build_url()
        params: Dict[str, Any] = {
            "force_publish": "false",
        }
        project_id = os.getenv("STRATEGIC_PROJECT_ID", "").strip()
        if project_id:
            params["project_id"] = project_id

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                params=params,
                headers=_headers(),
            )
            response.raise_for_status()
            payload = response.json()

        publication = payload.get("publication_status", {})
        logging.info(
            "Strategic autonomy cycle executed. published=%s reason=%s health_score=%s",
            publication.get("published"),
            publication.get("reason"),
            publication.get("health_score"),
        )
    except Exception as exc:
        logging.exception("Strategic autonomy timer failed: %s", exc)
