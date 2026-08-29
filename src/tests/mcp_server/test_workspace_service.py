"""
Tests for workspace MCP service behaviors.
"""

import json
import subprocess

import pytest

fastmcp = pytest.importorskip("fastmcp")

from core.factory import Domain  # noqa: E402
from services import workspace_service  # noqa: E402


@pytest.fixture
def workspace_tools(mock_mcp_server):
    """Register workspace tools and return them by function name."""
    service = workspace_service.WorkspaceToolService()
    service.register_tools(mock_mcp_server)

    return {
        tool["func"].__name__: tool["func"] for tool in mock_mcp_server.tools
    }, mock_mcp_server


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    """Point workspace resolution at a temporary root."""
    root = (tmp_path / "workspaces").resolve()
    root.mkdir()
    monkeypatch.setattr(workspace_service, "WORKSPACE_ROOT", root)
    return root


def _make_workspace(root, user_id="user-1", workspace_id="workspace-1"):
    workspace = root / user_id / workspace_id
    workspace.mkdir(parents=True)
    return workspace, user_id, workspace_id


def _init_git_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)


class TestWorkspaceToolService:
    """Test cases for workspace tools."""

    def test_register_tools(self, workspace_tools):
        """Test tool registration."""
        tools, mock_mcp_server = workspace_tools
        service = workspace_service.WorkspaceToolService()

        assert len(mock_mcp_server.tools) == service.tool_count
        assert "workspace_git_status" in tools
        assert "workspace_write_file" in tools
        for tool in mock_mcp_server.tools:
            assert Domain.WORKSPACE.value in tool["tags"]

    def test_workspace_git_status_returns_error_on_git_failure(
        self, workspace_root, workspace_tools, monkeypatch
    ):
        """Test git status surfaces git command failures."""
        tools, _ = workspace_tools
        _, user_id, workspace_id = _make_workspace(workspace_root)

        def mock_git(_ws, *args):
            assert args == ("status", "--short")
            return subprocess.CompletedProcess(
                ["git", *args], 128, stdout=b"", stderr=b"fatal: not a git repository"
            )

        monkeypatch.setattr(workspace_service, "_git", mock_git)

        result = tools["workspace_git_status"](user_id, workspace_id)

        assert "##### ❌ Error" in result
        assert "**Context:** workspace_git_status" in result
        assert "git status failed: fatal: not a git repository" in result

    def test_workspace_write_file_rejects_non_git_workspace(
        self, workspace_root, workspace_tools
    ):
        """Test writes are rejected before mutating a non-git workspace."""
        tools, _ = workspace_tools
        workspace, user_id, workspace_id = _make_workspace(workspace_root)

        result = tools["workspace_write_file"](
            user_id, workspace_id, "notes.txt", "hello"
        )

        assert "Workspace is not a git repository" in result
        assert not (workspace / "notes.txt").exists()

    def test_workspace_write_file_rejects_path_outside_workspace(
        self, workspace_root, workspace_tools
    ):
        """Test writes cannot escape the workspace root."""
        tools, _ = workspace_tools
        workspace, user_id, workspace_id = _make_workspace(workspace_root)
        _init_git_repo(workspace)

        result = tools["workspace_write_file"](
            user_id, workspace_id, "../escape.txt", "hello"
        )

        assert "Path outside workspace." in result
        assert not (workspace.parent / "escape.txt").exists()

    def test_workspace_write_file_rejects_content_over_size_limit(
        self, workspace_root, workspace_tools
    ):
        """Test writes over the max file size are rejected."""
        tools, _ = workspace_tools
        workspace, user_id, workspace_id = _make_workspace(workspace_root)
        _init_git_repo(workspace)
        content = "a" * (workspace_service.MAX_FILE_BYTES + 1)

        result = tools["workspace_write_file"](
            user_id, workspace_id, "large.txt", content
        )

        assert (
            f"File too large ({len(content)} bytes). Max is "
            f"{workspace_service.MAX_FILE_BYTES} bytes."
        ) in result
        assert not (workspace / "large.txt").exists()

    def test_workspace_write_file_writes_and_commits(
        self, workspace_root, workspace_tools
    ):
        """Test successful writes are committed immediately."""
        tools, _ = workspace_tools
        workspace, user_id, workspace_id = _make_workspace(workspace_root)
        _init_git_repo(workspace)

        result = tools["workspace_write_file"](
            user_id, workspace_id, "notes.txt", "hello", "agent: write notes.txt"
        )

        payload = json.loads(result)
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        commit_message = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )

        assert payload["status"] == "success"
        assert payload["action"] == "workspace_write_file"
        assert payload["details"] == {"path": "notes.txt", "bytes": 5}
        assert payload["summary"] == "Wrote 5 bytes to 'notes.txt' and committed."
        assert (workspace / "notes.txt").read_text(encoding="utf-8") == "hello"
        assert status.stdout.strip() == ""
        assert commit_message.stdout.strip() == "agent: write notes.txt"
