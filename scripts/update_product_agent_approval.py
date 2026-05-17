#!/usr/bin/env python3
"""Update Foundry agent MCP approval config via Azure AI Projects API.

This creates a new agent version. It does not edit router.py or local runtime
tool handling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "src" / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from azure.ai.projects.models import PromptAgentDefinition  # noqa: E402
from common.config.app_config import config  # noqa: E402


DEFAULT_AGENT_NAMES = ["ProductAgent"]
DEFAULT_MCP_LABEL = "MacaeMcpServer"

NEVER_REQUIRE_TOOLS = [
    "employee_onboarding_blueprint_flat",
    "initiate_background_check",
    "configure_laptop",
    "create_system_accounts",
    "schedule_orientation_session",
    "provide_employee_handbook",
    "register_for_benefits",
    "set_up_payroll",
    "request_id_card",
    "send_welcome_email",
    "assign_mentor",
    "set_up_office_365_account",
    "setup_vpn_access",
]


def _dedupe_sorted(values: list[str]) -> list[str]:
    return sorted({value for value in values if value})


def _summarize_tool(tool: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "type",
        "name",
        "server_label",
        "project_connection_id",
        "require_approval",
    ]
    return {key: tool.get(key) for key in keys if tool.get(key) is not None}


def _ensure_mcp_approval(
    tools: list[dict[str, Any]],
    *,
    mcp_label: str,
    project_connection_id: str,
    server_url: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Ensure MacaeMcpServer exists and has all needed tools in never approval."""
    changed = False
    mcp_tool: dict[str, Any] | None = None

    for tool in tools:
        if tool.get("type") == "mcp" and tool.get("server_label") == mcp_label:
            mcp_tool = tool
            break

    if mcp_tool is None:
        mcp_tool = {
            "type": "mcp",
            "server_label": mcp_label,
            "project_connection_id": project_connection_id,
        }
        if server_url:
            mcp_tool["server_url"] = server_url
        tools.append(mcp_tool)
        changed = True

    require_approval = mcp_tool.get("require_approval") or {}
    if require_approval == "never":
        current = list(NEVER_REQUIRE_TOOLS)
    elif isinstance(require_approval, dict):
        never = require_approval.get("never") or {}
        current = never.get("tool_names") or []
    else:
        current = []
    updated = _dedupe_sorted([*current, *NEVER_REQUIRE_TOOLS])

    if updated != current:
        mcp_tool["require_approval"] = {"never": {"tool_names": updated}}
        changed = True

    return tools, changed


async def update_agent(
    client: Any,
    *,
    agent_name: str,
    mcp_label: str,
    project_connection_id: str,
    server_url: str,
    dry_run: bool,
) -> None:
    agent = await client.agents.get(agent_name)
    agent_dict = agent.as_dict()
    latest = agent_dict["versions"]["latest"]
    definition = dict(latest.get("definition") or {})
    tools = [dict(tool) for tool in definition.get("tools", [])]

    print(f"\nAGENT {agent_name} current_version={latest.get('version')}")
    print(f"instructions_head={definition.get('instructions', '')[:160]}")

    tools, changed = _ensure_mcp_approval(
        tools,
        mcp_label=mcp_label,
        project_connection_id=project_connection_id,
        server_url=server_url,
    )
    definition["tools"] = tools

    for tool in tools:
        summary = _summarize_tool(tool)
        if summary.get("server_label") == mcp_label:
            print("target_mcp=" + json.dumps(summary, default=str))

    if not changed:
        print("No change needed.")
        return

    if dry_run:
        print("Dry run only. No new version created.")
        return

    created = await client.agents.create_version(
        agent_name=agent_name,
        description=latest.get("description") or agent_name,
        definition=PromptAgentDefinition(**definition),
    )
    print(
        f"Created new version: name={created.name} version={created.version} id={created.id}"
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", action="append", dest="agents")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mcp-label", default=os.getenv("MACAE_MCP_LABEL", DEFAULT_MCP_LABEL))
    parser.add_argument(
        "--project-connection-id",
        default=os.getenv("MACAE_MCP_PROJECT_CONNECTION_ID", DEFAULT_MCP_LABEL),
    )
    parser.add_argument("--server-url", default=os.getenv("MACAE_MCP_SERVER_URL", ""))
    args = parser.parse_args()

    client = config.get_ai_project_client()
    try:
        for agent_name in args.agents or DEFAULT_AGENT_NAMES:
            await update_agent(
                client,
                agent_name=agent_name,
                mcp_label=args.mcp_label,
                project_connection_id=args.project_connection_id,
                server_url=args.server_url,
                dry_run=args.dry_run,
            )
    finally:
        await client.close()
        credential = getattr(client, "_credential", None) or getattr(client, "credential", None)
        close = getattr(credential, "close", None)
        if close:
            result = close()
            if asyncio.iscoroutine(result):
                await result


if __name__ == "__main__":
    asyncio.run(main())
