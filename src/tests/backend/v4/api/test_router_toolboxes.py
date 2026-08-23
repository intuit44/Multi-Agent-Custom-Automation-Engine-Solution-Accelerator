import ast
import logging
import os
import sys
import types
from typing import Any, Optional


def _load_router_chat_client():
    router_path = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "..",
            "backend",
            "v4",
            "api",
            "router.py",
        )
    )
    with open(router_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=router_path)

    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_RouterChatClient"
    )
    namespace = {
        "Any": Any,
        "Optional": Optional,
        "logger": logging.getLogger("test-router-toolboxes"),
        "_DIRECT_RESPONSES_API_VERSION": "2025-03-01-preview",
    }
    exec(
        compile(ast.Module(body=[class_node], type_ignores=[]), router_path, "exec"),
        namespace,
    )
    return namespace["_RouterChatClient"]


def test_toolbox_parsing_and_headers_for_each_toolbox(monkeypatch):
    fake_config = types.SimpleNamespace(
        AZURE_AI_PROJECT_ENDPOINT="https://acct.example.com/api/projects/proj",
        CHAT_ORCHESTRATOR_MODEL="gpt-4.1",
        CHAT_TOOLBOXES="Sales:v1,Support,Ops:v2",
        MACAE_MCP_PUBLIC_ENDPOINT="",
        FOUNDRY_MCP_ENDPOINT="",
        FOUNDRY_MCP_SCOPE="",
        CHAT_ROUTER_MODEL="router-model",
        CHAT_ROUTER_API_VERSION="2024-10-21",
        _get_optional=lambda _name, default: default,
    )
    monkeypatch.setitem(sys.modules, "common", types.ModuleType("common"))
    monkeypatch.setitem(sys.modules, "common.config", types.ModuleType("common.config"))
    app_config_module = types.ModuleType("common.config.app_config")
    app_config_module.config = fake_config
    monkeypatch.setitem(sys.modules, "common.config.app_config", app_config_module)

    adapter_cls = _load_router_chat_client()
    adapter = adapter_cls("agent-name")

    assert adapter._toolboxes == [
        (
            "Sales",
            "https://acct.example.com/api/projects/proj/toolboxes/Sales/versions/v1/mcp?api-version=v1",
        ),
        (
            "Support",
            "https://acct.example.com/api/projects/proj/toolboxes/Support/mcp?api-version=v1",
        ),
        (
            "Ops",
            "https://acct.example.com/api/projects/proj/toolboxes/Ops/versions/v2/mcp?api-version=v1",
        ),
    ]

    tools = adapter._toolbox_tools("test-token")
    assert [tool["server_label"] for tool in tools] == ["Sales", "Support", "Ops"]
    assert [tool["server_url"] for tool in tools] == [
        "https://acct.example.com/api/projects/proj/toolboxes/Sales/versions/v1/mcp?api-version=v1",
        "https://acct.example.com/api/projects/proj/toolboxes/Support/mcp?api-version=v1",
        "https://acct.example.com/api/projects/proj/toolboxes/Ops/versions/v2/mcp?api-version=v1",
    ]
    assert all(
        tool["headers"].get("Foundry-Features") == "Toolboxes=V1Preview" for tool in tools
    )
