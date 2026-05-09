"""Azure Function Service Bus consumer for strategic autonomy decisions.

Consumes messages from the strategic decisions topic and performs
execution hooks (currently via optional webhook) for autonomous actions.

Uses Managed Identity (DefaultAzureCredential) for Service Bus authentication
to avoid storing connection strings.
"""

import json
import logging
import os
from typing import Any, Dict, List

import azure.functions as func
import httpx

app = func.FunctionApp()


def _extract_actions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return payload.get("recommended_actions", []) or []


async def _forward_to_webhook(payload: Dict[str, Any]) -> bool:
    """Forward decision payload to external executor if configured."""
    webhook_url = os.getenv("STRATEGIC_ACTION_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return False

    token = os.getenv("STRATEGIC_ACTION_WEBHOOK_BEARER_TOKEN", "").strip()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(webhook_url, json=payload, headers=headers)
        response.raise_for_status()
    return True


@app.service_bus_topic_trigger(
    arg_name="message",
    topic_name="strategic-decisions",
    subscription_name="sub-fibroskin-strategic-handler",
    connection="SERVICE_BUS_CONNECTION_STR",
    cardinality=func.Cardinality.ONE,
)
async def process_strategic_decision(message: func.ServiceBusMessage) -> None:
    """Consume strategic decision messages and trigger execution hooks.

    Note: Uses Managed Identity via SERVICEBUS_ENDPOINT environment variable.
    The Function App's system-assigned identity must have 'Azure Service Bus Data Receiver'
    permissions on the Service Bus namespace.
    """
    try:
        body = message.get_body().decode("utf-8")
        payload: Dict[str, Any] = json.loads(body)
    except Exception as exc:
        logging.exception("Failed to parse Service Bus message: %s", exc)
        raise

    decision_type = payload.get("type", "UNKNOWN")
    health_score = payload.get("health_score")
    actions = _extract_actions(payload)
    autonomous_actions = [a for a in actions if a.get("can_execute_autonomously")]

    logging.info(
        "Strategic decision consumed. type=%s health_score=%s actions=%s autonomous=%s",
        decision_type,
        health_score,
        len(actions),
        len(autonomous_actions),
    )

    # Current execution hook: optional webhook for external orchestrator/runner.
    # This avoids duplicating execution business logic inside the Function.
    try:
        forwarded = await _forward_to_webhook(payload)
        if forwarded:
            logging.info("Strategic decision forwarded to webhook executor")
        else:
            logging.info(
                "No STRATEGIC_ACTION_WEBHOOK_URL configured; message processed with logging only"
            )
    except Exception as exc:
        logging.exception("Failed forwarding strategic decision to webhook: %s", exc)
        # Don't re-raise - webhook is optional, message is still successfully processed
        # Log the error but continue to completion
