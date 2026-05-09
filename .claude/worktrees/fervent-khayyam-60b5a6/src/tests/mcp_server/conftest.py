"""
Test configuration for MCP server tests.
"""

import pytest
import sys
from pathlib import Path

# Repo root → needed for `from src.mcp_server.core.factory import …` in test files
repo_root = Path(__file__).resolve().parents[3]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

# MCP server package dir → needed for `from core.factory import …` in fixtures
mcp_server_path = repo_root / "src" / "mcp_server"
if str(mcp_server_path) not in sys.path:
    sys.path.insert(0, str(mcp_server_path))


@pytest.fixture
def mcp_factory():
    """Factory fixture for tests."""
    from core.factory import MCPToolFactory

    return MCPToolFactory()


@pytest.fixture
def hr_service():
    """HR service fixture."""
    from services.hr_service import HRService

    return HRService()


@pytest.fixture
def tech_support_service():
    """Tech support service fixture."""
    from services.tech_support_service import TechSupportService

    return TechSupportService()


@pytest.fixture
def general_service():
    """General service fixture."""
    from services.general_service import GeneralService

    return GeneralService()


@pytest.fixture
def mock_mcp_server():
    """Mock MCP server for testing."""

    class MockMCP:
        def __init__(self):
            self.tools = []

        def tool(self, tags=None):
            def decorator(func):
                self.tools.append({"func": func, "tags": tags or []})
                return func

            return decorator

    return MockMCP()
