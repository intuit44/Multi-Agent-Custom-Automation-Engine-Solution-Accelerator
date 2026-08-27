"""
Per-workspace file editing backed by a real Git repo — identical in dev and prod.

Architecture
------------
    frontend (Monaco)  ──►  /workspace/{workspace_id}/...   ─┐
    MCP filesystem / agents (future slice)                  ─┼──►  WorkspaceResolver
                                                             │     {WORKSPACE_ROOT}/{user_id}/{workspace_id}/
    git (init / commit / log / restore)  ◄───────────────────┘

The frontend only ever knows an identifier (today: the chat session_id); paths
never cross the wire as absolute — the server resolves them under the caller's
own workspace. Workspaces live in DATA (`MACAE_WORKSPACE_ROOT`), never in the
application source tree or the container image: dev defaults to
`~/.macae/workspaces`, prod points the env var at a mounted volume.

Each workspace is its own Git repository, created on first touch with an
initial empty commit so HEAD always exists (diff / log / restore never hit an
unborn branch).

Endpoints (all authenticated; user_id comes from EasyAuth headers)
------------------------------------------------------------------
GET  /workspace/{ws}/files            → list of files (path, size, mtime)
GET  /workspace/{ws}/files/{path}     → { path, content, size, mtime }
PUT  /workspace/{ws}/files/{path}     → write (creates parent dirs inside ws)
GET  /workspace/{ws}/diff/{path}      → git HEAD vs disk for Monaco DiffEditor
POST /workspace/{ws}/commit           → git add -A && commit → { sha }
GET  /workspace/{ws}/log              → last 50 commits
POST /workspace/{ws}/restore/{path}   → git checkout {ref} -- path

Security
--------
- Every request resolves under {root}/{user_id}/… — a user can only ever
  touch their own workspaces.
- workspace_id and user_id must match _SAFE_ID (no separators; a leading
  alphanumeric forbids "." / ".." / dotfiles), and file paths are hardened
  against traversal in _resolve.
- Binary files rejected (415); files over MAX_FILE_BYTES rejected (413).
- Missing git binary is an explicit 503, never a silent no-op.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth.auth_utils import get_authenticated_user_details

workspace_router = APIRouter(prefix="/workspace/{workspace_id}", tags=["workspace"])

# ── constants ────────────────────────────────────────────────────────────────
_SAFE_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

WORKSPACE_ROOT = Path(
    os.getenv("MACAE_WORKSPACE_ROOT", str(Path.home() / ".macae" / "workspaces"))
).resolve()
MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MB
MAX_LIST_ENTRIES = 1000

# Leading alphanumeric forbids dotfiles, "." and ".." outright; no separators.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
# Git refs for restore: leading alphanumeric forbids "-option" injection.
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./~^-]{0,63}$")

_GIT_IDENTITY = ("MACAE Workspace", "workspace@macae.local")
_init_lock = threading.Lock()


# ── helpers ──────────────────────────────────────────────────────────────────


def _auth_user(request: Request) -> str:
    try:
        details = get_authenticated_user_details(request_headers=request.headers)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    user_id = details.get("user_principal_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="no user found")
    return str(user_id)


def _git(ws: Path, *args: str) -> "subprocess.CompletedProcess[bytes]":
    try:
        return subprocess.run(["git", *args], cwd=ws, capture_output=True, timeout=15)
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="git is not installed in this image; workspaces need it.",
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="git operation timed out.")


def _validated_id(value: str, field_name: str) -> str:
    candidate = value.strip()
    if (
        not candidate
        or candidate in {".", ".."}
        or "/" in candidate
        or "\\" in candidate
        or not _SAFE_ID.match(candidate)
    ):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}.")
    return candidate


def _validated_workspace_id(value: str) -> str:
    """Return a validated workspace id safe for filesystem path composition."""
    candidate = value.strip()
    if (
        not candidate
        or candidate in {".", ".."}
        or "/" in candidate
        or "\\" in candidate
        or not _SAFE_WORKSPACE_ID.match(candidate)
    ):
        raise HTTPException(status_code=400, detail="Invalid workspace id.")
    return candidate


def _validated_segment(value: str, field_name: str) -> str:
    """Return a validated single path segment."""
    candidate = _validated_id(value, field_name)
    if field_name == "workspace id" and not _SAFE_WORKSPACE_ID.match(candidate):
        raise HTTPException(status_code=400, detail="Invalid workspace id.")
    return candidate


def _validated_rel_path(raw: str) -> Path:
    """Return a validated relative path (workspace-local)."""
    rel_path = Path(raw.replace("\\", "/").strip())
    if str(rel_path) in {"", "."} or rel_path.is_absolute():
        raise HTTPException(status_code=400, detail="Path outside workspace.")
    if ".." in rel_path.parts or "." in rel_path.parts or "" in rel_path.parts:
        raise HTTPException(status_code=400, detail="Path outside workspace.")
    return rel_path


def _resolve_under(base: Path, *parts: str) -> Path:
    """Resolve a path under base and reject escapes outside base."""
    resolved_base = base.resolve()
    resolved_path = (resolved_base.joinpath(*parts)).resolve()
    try:
        resolved_path.relative_to(resolved_base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid workspace path.")
    return resolved_path


def _workspace_for(request: Request, workspace_id: str) -> Path:
    """Resolve (and lazily create) the caller's workspace directory."""
    user_id = _validated_segment(_auth_user(request), "user id")
    safe_workspace_id = _validated_workspace_id(workspace_id)
    root = WORKSPACE_ROOT.resolve()
    user_root = _resolve_under(root, user_id)
    ws = _resolve_under(user_root, safe_workspace_id)
    if not (ws / ".git").is_dir():
        with _init_lock:
            if not (ws / ".git").is_dir():  # re-check under the lock
                try:
                    ws.parent.resolve().relative_to(root)
                except ValueError:
                    raise HTTPException(
                        status_code=400, detail="Invalid workspace path."
                    )
                ws.mkdir(parents=True, exist_ok=True)
                ws = ws.resolve()
                try:
                    ws.relative_to(root)
                except ValueError:
                    raise HTTPException(
                        status_code=400, detail="Invalid workspace path."
                    )
                for cmd in (
                    ("init", "-q"),
                    ("config", "user.name", _GIT_IDENTITY[0]),
                    ("config", "user.email", _GIT_IDENTITY[1]),
                    ("commit", "--allow-empty", "-q", "-m", "init workspace"),
                ):
                    result = _git(ws, *cmd)
                    if result.returncode != 0:
                        raise HTTPException(
                            status_code=500,
                            detail="Workspace git init failed: "
                            + result.stderr.decode("utf-8", errors="replace"),
                        )
    return ws


def _resolve(ws: Path, raw: str) -> Path:
    """Resolve *raw* relative to the workspace; reject traversal."""
    rel_path = _validated_rel_path(raw)
    ws_root = ws.resolve(strict=False)
    candidate = (ws_root / rel_path).resolve(strict=False)
    try:
        rel = candidate.relative_to(ws_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path outside workspace.")
    if ".git" in rel.parts:
        raise HTTPException(status_code=400, detail="The .git directory is managed.")
    return candidate


def _ensure_within_workspace(ws: Path, candidate: Path) -> Path:
    """Final guard for filesystem sinks: ensure candidate is within workspace."""
    try:
        ws_resolved = ws.resolve(strict=True)
        candidate_resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found.")
    try:
        candidate_resolved.relative_to(ws_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path outside workspace.")
    return candidate_resolved


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _read_text_guarded(ws: Path, resolved: Path, path: str) -> bytes:
    resolved = _ensure_within_workspace(ws, resolved)
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="Path is a directory.")
    raw = resolved.read_bytes()
    if _is_binary(raw):
        raise HTTPException(
            status_code=415,
            detail="Binary files cannot be edited in Monaco. Use the download button.",
        )
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds {MAX_FILE_BYTES // 1024} KB Monaco limit.",
        )
    return raw


# ── models ───────────────────────────────────────────────────────────────────


class FileInfo(BaseModel):
    path: str
    size: int
    mtime: float


class FileListResponse(BaseModel):
    workspace_id: str
    files: list[FileInfo]
    truncated: bool


class FileResponse(BaseModel):
    path: str
    content: str
    size: int
    mtime: float


class FilePutRequest(BaseModel):
    content: str


class FilePutResponse(BaseModel):
    path: str
    size: int
    mtime: float


class DiffResponse(BaseModel):
    path: str
    original: str  # git HEAD (empty string if untracked)
    modified: str  # current disk content


class CommitRequest(BaseModel):
    message: str = "Update workspace"


class CommitResponse(BaseModel):
    committed: bool
    sha: str
    message: str


class LogEntry(BaseModel):
    sha: str
    author: str
    date: str
    message: str


class LogResponse(BaseModel):
    workspace_id: str
    commits: list[LogEntry]


class RestoreRequest(BaseModel):
    ref: str = "HEAD"


# ── endpoints ────────────────────────────────────────────────────────────────


@workspace_router.get("/files", response_model=FileListResponse)
def list_files(request: Request, workspace_id: str) -> FileListResponse:
    """List every file in the workspace (excluding .git)."""
    ws = _workspace_for(request, workspace_id)
    files: list[FileInfo] = []
    truncated = False
    for root, dirs, names in os.walk(ws):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in sorted(names):
            candidate = Path(root) / name
            full = _ensure_within_workspace(ws, candidate)
            stat = full.stat()
            files.append(
                FileInfo(
                    path=str(full.relative_to(ws)),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
            if len(files) >= MAX_LIST_ENTRIES:
                truncated = True
                break
        if truncated:
            break
    files.sort(key=lambda f: f.path)
    return FileListResponse(workspace_id=workspace_id, files=files, truncated=truncated)


@workspace_router.get("/files/{path:path}", response_model=FileResponse)
def get_file(request: Request, workspace_id: str, path: str) -> FileResponse:
    """Read a workspace file as text."""
    ws = _workspace_for(request, workspace_id)
    resolved = _resolve(ws, path)
    raw = _read_text_guarded(ws, resolved, path)
    stat = resolved.stat()
    return FileResponse(
        path=path,
        content=raw.decode("utf-8", errors="replace"),
        size=stat.st_size,
        mtime=stat.st_mtime,
    )


@workspace_router.put("/files/{path:path}", response_model=FilePutResponse)
def put_file(
    request: Request, workspace_id: str, path: str, body: FilePutRequest
) -> FilePutResponse:
    """Write a workspace file (parent directories are created inside the ws)."""
    ws = _workspace_for(request, workspace_id)
    resolved = _resolve(ws, path)
    try:
        resolved.relative_to(ws)
        resolved.parent.relative_to(ws)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path outside workspace.")
    if resolved.exists() and not resolved.is_file():
        raise HTTPException(status_code=400, detail="Path is a directory.")
    encoded = body.content.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Content exceeds {MAX_FILE_BYTES // 1024} KB limit.",
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(encoded)
    stat = resolved.stat()
    return FilePutResponse(path=path, size=stat.st_size, mtime=stat.st_mtime)


@workspace_router.get("/diff/{path:path}", response_model=DiffResponse)
def get_diff(request: Request, workspace_id: str, path: str) -> DiffResponse:
    """Return git HEAD vs disk for Monaco DiffEditor."""
    ws = _workspace_for(request, workspace_id)
    resolved = _resolve(ws, path)
    raw = _read_text_guarded(ws, resolved, path)
    rel = str(resolved.relative_to(ws))
    show = _git(ws, "show", f"HEAD:{rel}")
    head_content = (
        show.stdout.decode("utf-8", errors="replace") if show.returncode == 0 else ""
    )
    return DiffResponse(
        path=path,
        original=head_content,
        modified=raw.decode("utf-8", errors="replace"),
    )


@workspace_router.post("/commit", response_model=CommitResponse)
def commit(request: Request, workspace_id: str, body: CommitRequest) -> CommitResponse:
    """Stage everything and commit; no-op (committed=false) when clean."""
    ws = _workspace_for(request, workspace_id)
    add = _git(ws, "add", "-A")
    if add.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail="git add failed: " + add.stderr.decode("utf-8", errors="replace"),
        )
    head_res = _git(ws, "rev-parse", "HEAD")
    if head_res.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail="git rev-parse failed: "
            + head_res.stderr.decode("utf-8", errors="replace"),
        )
    head = head_res.stdout.decode("utf-8", errors="replace").strip()

    staged = _git(ws, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        return CommitResponse(committed=False, sha=head, message="")
    if staged.returncode != 1:
        raise HTTPException(
            status_code=500,
            detail="git diff --cached failed: "
            + staged.stderr.decode("utf-8", errors="replace"),
        )
    message = body.message.strip() or "Update workspace"
    result = _git(ws, "commit", "-q", "-m", message)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail="git commit failed: "
            + result.stderr.decode("utf-8", errors="replace"),
        )
    sha = _git(ws, "rev-parse", "HEAD").stdout.decode().strip()
    return CommitResponse(committed=True, sha=sha, message=message)


@workspace_router.get("/log", response_model=LogResponse)
def log(request: Request, workspace_id: str) -> LogResponse:
    """Last 50 commits of this workspace."""
    ws = _workspace_for(request, workspace_id)
    result = _git(ws, "log", "-n", "50", "--pretty=format:%H%x1f%an%x1f%aI%x1f%s")
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail="git log failed: " + result.stderr.decode("utf-8", errors="replace"),
        )
    commits: list[LogEntry] = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append(
                LogEntry(sha=parts[0], author=parts[1], date=parts[2], message=parts[3])
            )
    return LogResponse(workspace_id=workspace_id, commits=commits)


@workspace_router.post("/restore/{path:path}", response_model=FileResponse)
def restore(
    request: Request, workspace_id: str, path: str, body: RestoreRequest
) -> FileResponse:
    """Restore one file from a ref (default HEAD) and return its content."""
    ws = _workspace_for(request, workspace_id)
    resolved = _resolve(ws, path)
    resolved = _ensure_within_workspace(ws, resolved)
    ref = body.ref.strip() or "HEAD"
    if not _SAFE_REF.match(ref):
        raise HTTPException(status_code=400, detail="Invalid git ref.")
    rel = str(resolved.relative_to(ws))
    result = _git(ws, "checkout", "-q", ref, "--", rel)
    if result.returncode != 0:
        raise HTTPException(
            status_code=404,
            detail=f"Cannot restore {path} from {ref}: "
            + result.stderr.decode("utf-8", errors="replace"),
        )
    raw = _read_text_guarded(ws, resolved, path)
    stat = resolved.stat()
    return FileResponse(
        path=path,
        content=raw.decode("utf-8", errors="replace"),
        size=stat.st_size,
        mtime=stat.st_mtime,
    )
