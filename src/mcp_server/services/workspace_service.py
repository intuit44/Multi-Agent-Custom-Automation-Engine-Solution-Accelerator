"""
Workspace tools — the agents' EYES and HANDS on the per-user workspace.

Self-contained twin of the backend resolver (same pattern as
credential_resolver: ca-mcp is a separate process/container, so it operates
directly on the SHARED volume instead of importing backend code):

    {MACAE_WORKSPACE_ROOT}/{user_id}/{workspace_id}/

Read tools (3): workspace_list_entries, workspace_read_file, workspace_search_files
Git-read tools (6): workspace_search_content, workspace_git_status,
    workspace_git_diff, workspace_git_log, workspace_git_current_branch,
    workspace_git_list_branches
Write tools (4): workspace_write_file, workspace_create_file,
    workspace_update_file, workspace_delete_file
  Write tools commit every change to git automatically (message auto-generated
  from the operation).  The write helpers require the workspace to already exist
  (initiated by the backend); they never create the root git repo.

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
_GIT_IDENTITY = ("MACAE Workspace", "workspace@macae.local")


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


def _workspace_dir_write(user_id: str, workspace_id: str) -> Path:
    """Like _workspace_dir but used by write tools — same resolution, explicit
    name makes call-sites clearer about intent."""
    ws = _workspace_dir(user_id, workspace_id)
    if _git(ws, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise WorkspaceAccessError(
            "Workspace is not a git repository; it must be initialized by the backend first."
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


def _git_commit_all(ws: Path, message: str) -> None:
    """Stage all changes in *ws* and commit with *message*. Idempotent: if
    nothing changed after staging, the commit is skipped gracefully."""
    _git(ws, "config", "user.name", _GIT_IDENTITY[0])
    _git(ws, "config", "user.email", _GIT_IDENTITY[1])
    _git(ws, "add", "-A")
    result = _git(ws, "diff", "--cached", "--quiet")
    if result.returncode == 0:
        return  # nothing staged — no commit needed
    commit_result = _git(ws, "commit", "-q", "-m", message)
    if commit_result.returncode != 0:
        raise WorkspaceAccessError(
            "git commit failed: "
            + commit_result.stderr.decode("utf-8", errors="replace").strip()
        )


class WorkspaceToolService(MCPToolBase):
    """Workspace tools for agents — list, read, search, git-read, and write."""

    def __init__(self):
        super().__init__(Domain.WORKSPACE)

    def register_tools(self, mcp) -> None:
        # ── READ TOOLS ────────────────────────────────────────────────────

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

        # ── GIT-READ TOOLS ────────────────────────────────────────────────

        @mcp.tool(tags={self.domain.value})
        def workspace_search_content(
            user_id: str, workspace_id: str, pattern: str, path: str = ""
        ) -> str:
            """Grep for *pattern* (literal string, case-insensitive) inside tracked
            and untracked files. Optionally restrict to a sub-path (relative to
            the workspace root). Returns up to 200 matching lines as
            'rel/path:lineno:text'. Returns an empty list when nothing matches."""
            try:
                ws = _workspace_dir(user_id, workspace_id)
                if not pattern:
                    raise WorkspaceAccessError("Empty pattern.")
                # git grep --untracked searches tracked + untracked files.
                # --no-index is INCOMPATIBLE with --untracked (git fatal error).
                # Pathspec must be RELATIVE to the repo root, not an absolute path.
                args = ["grep", "-i", "-n", "--untracked", "-e", pattern]
                if path:
                    # Resolve to validate containment, then make relative.
                    resolved = _resolve_in(ws, path)
                    rel = str(resolved.relative_to(ws))
                    args += ["--", rel]
                result = _git(ws, *args)
                # rc=0 → matches found; rc=1 → no matches (not an error); rc>1 → error
                if result.returncode > 1:
                    stderr = result.stderr.decode("utf-8", errors="replace").strip()
                    raise WorkspaceAccessError(f"git grep failed: {stderr}")
                lines = result.stdout.decode("utf-8", errors="replace").splitlines()
                rel_lines = lines[:MAX_ENTRIES]
                return format_success_response(
                    action="workspace_search_content",
                    details={
                        "pattern": pattern,
                        "path": path or "/",
                        "matches": rel_lines,
                    },
                    summary=f"{len(rel_lines)} matching lines for '{pattern}'.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_search_content"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_search_content"
                )

        @mcp.tool(tags={self.domain.value})
        def workspace_git_status(user_id: str, workspace_id: str) -> str:
            """Return the short git status of the workspace (staged, unstaged,
            untracked files). Equivalent to `git status --short`."""
            try:
                ws = _workspace_dir(user_id, workspace_id)
                result = _git(ws, "status", "--short")
                output = result.stdout.decode("utf-8", errors="replace").strip()
                return format_success_response(
                    action="workspace_git_status",
                    details={"status": output or "(clean)"},
                    summary="Git status retrieved.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_git_status"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_git_status"
                )

        @mcp.tool(tags={self.domain.value})
        def workspace_git_diff(
            user_id: str, workspace_id: str, path: str = "", staged: bool = False
        ) -> str:
            """Show the diff of uncommitted changes. Set staged=true to see
            staged (indexed) changes. Optionally restrict to a sub-path.
            Output is capped at 64 KB."""
            try:
                ws = _workspace_dir(user_id, workspace_id)
                args = ["diff"]
                if staged:
                    args.append("--cached")
                if path:
                    target = _resolve_in(ws, path)
                    args += ["--", str(target)]
                result = _git(ws, *args)
                output = result.stdout.decode("utf-8", errors="replace")
                if len(output) > 65536:
                    output = output[:65536] + "\n... (truncated)"
                return format_success_response(
                    action="workspace_git_diff",
                    details={
                        "staged": staged,
                        "path": path or "/",
                        "diff": output or "(no changes)",
                    },
                    summary="Git diff retrieved.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_git_diff"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_git_diff"
                )

        @mcp.tool(tags={self.domain.value})
        def workspace_git_log(
            user_id: str, workspace_id: str, max_entries: int = 20
        ) -> str:
            """Return the last *max_entries* (capped at 100) git commits in the
            workspace as a list of {hash, author, date, message}."""
            try:
                ws = _workspace_dir(user_id, workspace_id)
                n = max(1, min(max_entries, 100))
                result = _git(
                    ws,
                    "log",
                    f"-{n}",
                    "--pretty=format:%H\x1f%an\x1f%ai\x1f%s",
                )
                entries = []
                for line in result.stdout.decode(
                    "utf-8", errors="replace"
                ).splitlines():
                    parts = line.split("\x1f", 3)
                    if len(parts) == 4:
                        entries.append(
                            {
                                "hash": parts[0][:12],
                                "author": parts[1],
                                "date": parts[2],
                                "message": parts[3],
                            }
                        )
                return format_success_response(
                    action="workspace_git_log",
                    details={"entries": entries},
                    summary=f"{len(entries)} commit(s) retrieved.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_git_log"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_git_log"
                )

        @mcp.tool(tags={self.domain.value})
        def workspace_git_current_branch(user_id: str, workspace_id: str) -> str:
            """Return the name of the currently checked-out branch (or the
            detached HEAD SHA if not on a branch)."""
            try:
                ws = _workspace_dir(user_id, workspace_id)
                result = _git(ws, "symbolic-ref", "--short", "HEAD")
                if result.returncode == 0:
                    branch = result.stdout.decode("utf-8", errors="replace").strip()
                else:
                    rev = _git(ws, "rev-parse", "--short", "HEAD")
                    branch = (
                        "(detached) "
                        + rev.stdout.decode("utf-8", errors="replace").strip()
                    )
                return format_success_response(
                    action="workspace_git_current_branch",
                    details={"branch": branch},
                    summary=f"Current branch: {branch}",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_git_current_branch"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_git_current_branch"
                )

        @mcp.tool(tags={self.domain.value})
        def workspace_git_list_branches(user_id: str, workspace_id: str) -> str:
            """List all local branches in the workspace. The active branch is
            marked with a leading '*'."""
            try:
                ws = _workspace_dir(user_id, workspace_id)
                result = _git(ws, "branch", "--list")
                branches = [
                    line.strip()
                    for line in result.stdout.decode(
                        "utf-8", errors="replace"
                    ).splitlines()
                    if line.strip()
                ]
                return format_success_response(
                    action="workspace_git_list_branches",
                    details={"branches": branches},
                    summary=f"{len(branches)} branch(es) found.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_git_list_branches"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_git_list_branches"
                )

        # ── WRITE TOOLS ───────────────────────────────────────────────────

        @mcp.tool(tags={self.domain.value})
        def workspace_write_file(
            user_id: str,
            workspace_id: str,
            path: str,
            content: str,
            commit_message: str = "",
        ) -> str:
            """Write (create or overwrite) a text file at *path* inside the
            workspace and commit the change. Use workspace_create_file when you
            want an explicit guard against overwriting. commit_message is
            optional — a default is generated from the path."""
            try:
                ws = _workspace_dir_write(user_id, workspace_id)
                dest = _resolve_in(ws, path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
                msg = commit_message.strip() or f"agent: write {path}"
                _git_commit_all(ws, msg)
                return format_success_response(
                    action="workspace_write_file",
                    details={"path": path, "bytes": len(content.encode("utf-8"))},
                    summary=f"Wrote {len(content.encode('utf-8'))} bytes to '{path}' and committed.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_write_file"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_write_file"
                )

        @mcp.tool(tags={self.domain.value})
        def workspace_create_file(
            user_id: str,
            workspace_id: str,
            path: str,
            content: str,
            commit_message: str = "",
        ) -> str:
            """Create a NEW text file at *path* inside the workspace and commit.
            Fails if the file already exists — use workspace_write_file to
            overwrite."""
            try:
                ws = _workspace_dir_write(user_id, workspace_id)
                dest = _resolve_in(ws, path)
                if dest.exists():
                    raise WorkspaceAccessError(
                        f"File already exists: '{path}'. Use workspace_write_file to overwrite."
                    )
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
                msg = commit_message.strip() or f"agent: create {path}"
                _git_commit_all(ws, msg)
                return format_success_response(
                    action="workspace_create_file",
                    details={"path": path, "bytes": len(content.encode("utf-8"))},
                    summary=f"Created '{path}' ({len(content.encode('utf-8'))} bytes) and committed.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_create_file"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_create_file"
                )

        @mcp.tool(tags={self.domain.value})
        def workspace_update_file(
            user_id: str,
            workspace_id: str,
            path: str,
            content: str,
            commit_message: str = "",
        ) -> str:
            """Update an EXISTING text file at *path* with new *content* and
            commit. Fails if the file does not exist — use workspace_create_file
            to create it first."""
            try:
                ws = _workspace_dir_write(user_id, workspace_id)
                dest = _resolve_in(ws, path)
                if not dest.exists():
                    raise WorkspaceAccessError(
                        f"File not found: '{path}'. Use workspace_create_file to create it."
                    )
                if not dest.is_file():
                    raise WorkspaceAccessError(f"Path is a directory: '{path}'.")
                dest.write_text(content, encoding="utf-8")
                msg = commit_message.strip() or f"agent: update {path}"
                _git_commit_all(ws, msg)
                return format_success_response(
                    action="workspace_update_file",
                    details={"path": path, "bytes": len(content.encode("utf-8"))},
                    summary=f"Updated '{path}' ({len(content.encode('utf-8'))} bytes) and committed.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_update_file"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_update_file"
                )

        @mcp.tool(tags={self.domain.value})
        def workspace_delete_file(
            user_id: str,
            workspace_id: str,
            path: str,
            commit_message: str = "",
        ) -> str:
            """Delete a file (or empty directory) at *path* from the workspace
            and commit the removal. Directories are only deleted when empty."""
            try:
                ws = _workspace_dir_write(user_id, workspace_id)
                target = _resolve_in(ws, path)
                if not target.exists():
                    raise WorkspaceAccessError(f"Path not found: '{path}'.")
                if target.is_dir():
                    if any(target.iterdir()):
                        raise WorkspaceAccessError(
                            f"Directory '{path}' is not empty. Delete its contents first."
                        )
                    target.rmdir()
                else:
                    target.unlink()
                msg = commit_message.strip() or f"agent: delete {path}"
                _git_commit_all(ws, msg)
                return format_success_response(
                    action="workspace_delete_file",
                    details={"path": path},
                    summary=f"Deleted '{path}' and committed.",
                )
            except WorkspaceAccessError as e:
                return format_error_response(
                    error_message=str(e), context="workspace_delete_file"
                )
            except Exception as e:
                return format_error_response(
                    error_message=str(e), context="workspace_delete_file"
                )

    @property
    def tool_count(self) -> int:
        return 13
