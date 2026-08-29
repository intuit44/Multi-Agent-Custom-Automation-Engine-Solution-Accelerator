"""
Workspace read tools — the agents' EYES on the per-user workspace.

Self-contained twin of the backend resolver (same pattern as
credential_resolver: ca-mcp is a separate process/container, so it operates
directly on the SHARED volume instead of importing backend code):

    {MACAE_WORKSPACE_ROOT}/{user_id}/{workspace_id}/

READ-ONLY by design: writing already happens through the backend's automatic
code-interpreter materialisation; these tools never create, never modify.
Local dev shares the machine with the backend (same default root); in prod
ca-mcp mounts the same Azure Files share (`macae-workspaces`) at the same
MACAE_WORKSPACE_ROOT path — operator step, one extra mount.

Security: identical containment forms as the backend service — normpath +
SIMPLE `startswith(base + os.sep)` guards (the only shape CodeQL recognizes
as a py/path-injection barrier; compound conditions break recognition),
followed by a symlink-collapsing resolve + re-check. Linked workspaces
(symlinks under the user root) resolve to their target like the backend does.
"""

import os
import re
import subprocess
from pathlib import Path

from core.factory import Domain, MCPToolBase
from utils.formatters import format_error_response, format_success_response

WORKSPACE_ROOT = Path(
    os.getenv("MACAE_WORKSPACE_ROOT") or str(Path.home() / ".macae" / "workspaces")
).resolve()
MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MB — same read cap as the backend
MAX_ENTRIES = 200
_META_FILE = ".macae_workspace_meta.json"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")


class WorkspaceAccessError(Exception):
    """Raised for any invalid/denied workspace access; message is user-safe."""


def _workspace_dir(user_id: str, workspace_id: str) -> Path:
    """Resolve an EXISTING workspace (read-only: never creates)."""
    if not user_id or not _SAFE_ID.match(user_id):
        raise WorkspaceAccessError(f"Invalid user_id: '{user_id}'.")
    if not workspace_id or not _SAFE_ID.match(workspace_id):
        raise WorkspaceAccessError(f"Invalid workspace_id: '{workspace_id}'.")
    joined = os.path.normpath(os.path.join(str(WORKSPACE_ROOT), user_id, workspace_id))
    if not joined.startswith(str(WORKSPACE_ROOT) + os.sep):
        raise WorkspaceAccessError("Workspace path escapes the root.")
    ws = Path(joined)
    if ws.is_symlink():  # linked workspace → operate on the real project
        if os.getenv("APP_ENV", "dev") != "dev":
            raise WorkspaceAccessError("Linked workspaces are dev-only.")
        link_root = Path(os.getenv("MACAE_LINK_ROOT", "/workspaces")).resolve()
        target = ws.resolve()
        if not str(target).startswith(str(link_root) + os.sep):
            raise WorkspaceAccessError("Linked workspace escapes the link root.")
        ws = target
    elif not ws.is_dir():
        raise WorkspaceAccessError(
            f"Workspace '{workspace_id}' does not exist for this user."
        )
    return ws


def _resolve_in(ws: Path, raw: str) -> Path:
    ws = ws.resolve()
    rel = Path((raw or "").replace("\\", "/").strip())
    if str(rel) in {"", "."}:
        return ws
    if rel.is_absolute() or ".." in rel.parts:
        raise WorkspaceAccessError("Path outside workspace.")
    joined = os.path.normpath(os.path.join(str(ws), str(rel)))
    if not joined.startswith(str(ws) + os.sep):
        raise WorkspaceAccessError("Path outside workspace.")
    resolved = Path(joined).resolve()
    if not str(resolved).startswith(str(ws) + os.sep):
        raise WorkspaceAccessError("Path outside workspace.")
    if ".git" in resolved.relative_to(ws).parts:
        raise WorkspaceAccessError("The .git directory is managed.")
    return resolved


def _git(ws: Path, *args: str) -> "subprocess.CompletedProcess[bytes]":
    try:
        return subprocess.run(["git", *args], cwd=ws, capture_output=True, timeout=15)
    except FileNotFoundError as exc:
        raise WorkspaceAccessError("git is not available in this environment.") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceAccessError("git operation timed out.") from exc


class WorkspaceToolService(MCPToolBase):
    """Read-only workspace access for agents (list / read / search)."""

    def __init__(self):
        super().__init__(Domain.WORKSPACE)

    def register_tools(self, mcp) -> None:
        @mcp.tool(tags={self.domain.value})
        def workspace_list_entries(
            user_id: str, workspace_id: str, path: str = ""
        ) -> str:
            """List ONE directory level of the user's project workspace
            (directories first). Call with path='' for the root, then with a
            directory path to descend. user_id and workspace_id are MANDATORY
            — use the values given in your instructions."""
            try:
                ws = _workspace_dir(user_id, workspace_id)
                base = _resolve_in(ws, path)
                if not base.is_dir():
                    raise WorkspaceAccessError(f"Not a directory: {path}")
                dirs: list[dict] = []
                files: list[dict] = []
                for entry in sorted(base.iterdir(), key=lambda e: e.name.lower()):
                    if entry.name == ".git" or entry.name == _META_FILE:
                        continue
                    if len(dirs) + len(files) >= MAX_ENTRIES:
                        break
                    if entry.is_dir():
                        dirs.append({"name": entry.name, "type": "directory"})
                    else:
                        files.append(
                            {
                                "name": entry.name,
                                "type": "file",
                                "size": entry.stat().st_size,
                            }
                        )
                return format_success_response(
                    action="workspace_list_entries",
                    details={"path": path or "/", "entries": dirs + files},
                    summary=f"{len(dirs)} directories, {len(files)} files in "
                    f"'{path or '/'}' of workspace '{workspace_id}'.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_list_entries"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_list_entries"
                )

        @mcp.tool(tags={self.domain.value})
        def workspace_read_file(user_id: str, workspace_id: str, path: str) -> str:
            """Read a TEXT file from the user's project workspace and return its
            full content. Binary files and files over 1 MB are refused. user_id
            and workspace_id are MANDATORY — use the values from your
            instructions."""
            try:
                ws = _workspace_dir(user_id, workspace_id)
                resolved = _resolve_in(ws, path)
                if not resolved.is_file():
                    raise WorkspaceAccessError(f"File not found: {path}")
                raw = resolved.read_bytes()
                if b"\x00" in raw[:8192]:
                    raise WorkspaceAccessError(f"Binary file (cannot read): {path}")
                if len(raw) > MAX_FILE_BYTES:
                    raise WorkspaceAccessError(f"File exceeds 1 MB limit: {path}")
                return format_success_response(
                    action="workspace_read_file",
                    details={
                        "path": path,
                        "content": raw.decode("utf-8", errors="replace"),
                    },
                    summary=f"Read {len(raw)} bytes from '{path}'.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_read_file"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_read_file"
                )

        @mcp.tool(tags={self.domain.value})
        def workspace_search_files(user_id: str, workspace_id: str, query: str) -> str:
            """Find files by name across the WHOLE workspace (case-insensitive
            substring on the relative path; git is the index). Returns up to
            200 matching paths. user_id and workspace_id are MANDATORY."""
            try:
                ws = _workspace_dir(user_id, workspace_id)
                q = (query or "").strip().lower()
                if not q:
                    raise WorkspaceAccessError("Empty query.")
                names: set[str] = set()
                for extra in ((), ("--others", "--exclude-standard")):
                    result = _git(ws, "ls-files", *extra)
                    if result.returncode == 0:
                        names.update(
                            result.stdout.decode("utf-8", errors="replace").splitlines()
                        )
                names.discard(_META_FILE)
                matches = sorted(p for p in names if q in p.lower())[:MAX_ENTRIES]
                return format_success_response(
                    action="workspace_search_files",
                    details={"query": query, "matches": matches},
                    summary=f"{len(matches)} files match '{query}' in "
                    f"workspace '{workspace_id}'.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_search_files"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_search_files"
                )

    @property
    def tool_count(self) -> int:
        return 3
