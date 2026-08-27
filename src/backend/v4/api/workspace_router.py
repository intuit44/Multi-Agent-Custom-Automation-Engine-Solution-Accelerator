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
  alphanumeric forbids "." / ".." / dotfiles).
- File paths are contained twice in _resolve: an os.path.normpath +
  startswith prefix guard (the sanitizer form CodeQL's py/path-injection
  analysis recognizes), then a symlink-collapsing resolve + relative_to
  re-check. Do not "simplify" either away.
- Binary files rejected (415); files over MAX_FILE_BYTES rejected (413).
- Missing git binary is an explicit 503, never a silent no-op.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from auth.auth_utils import get_authenticated_user_details

# Router for per-workspace file operations  (/workspace/{workspace_id}/…)
workspace_router = APIRouter(prefix="/workspace/{workspace_id}", tags=["workspace"])
# Router for workspace management  (/workspaces  and  /workspaces/{workspace_id})
workspaces_router = APIRouter(prefix="/workspaces", tags=["workspaces"])

# ── constants ────────────────────────────────────────────────────────────────

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


def _contained(base: Path, *parts: str) -> Path:
    """Join *parts* under *base* with the normpath + startswith containment
    guard — the sanitizer form CodeQL recognizes as a py/path-injection
    barrier. Callers must pre-validate parts semantically; this is the single
    choke point every request-derived path flows through."""
    joined = os.path.normpath(os.path.join(str(base), *parts))
    if not joined.startswith(str(base) + os.sep):
        raise HTTPException(status_code=400, detail="Path outside workspace.")
    return Path(joined)


def _workspace_for(request: Request, workspace_id: str) -> Path:
    """Resolve (and lazily create) the caller's workspace directory."""
    user_id = _auth_user(request)
    if not _SAFE_ID.match(user_id):
        raise HTTPException(status_code=400, detail="Invalid user id.")
    if not _SAFE_ID.match(workspace_id):
        raise HTTPException(status_code=400, detail="Invalid workspace id.")
    ws = _contained(WORKSPACE_ROOT, user_id, workspace_id)
    if not (ws / ".git").is_dir():
        with _init_lock:
            if not (ws / ".git").is_dir():  # re-check under the lock
                ws.mkdir(parents=True, exist_ok=True)
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
    rel_path = Path(raw.replace("\\", "/").strip())
    if str(rel_path) in {"", "."} or rel_path.is_absolute() or ".." in rel_path.parts:
        raise HTTPException(status_code=400, detail="Path outside workspace.")
    candidate = _contained(ws, str(rel_path))
    # Second, stronger runtime check: collapse symlinks and re-verify
    # containment (normpath alone does not follow symlinks).
    resolved = candidate.resolve()
    try:
        rel = resolved.relative_to(ws)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path outside workspace.")
    if ".git" in rel.parts:
        raise HTTPException(status_code=400, detail="The .git directory is managed.")
    return resolved


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _read_text_guarded(resolved: Path, path: str) -> bytes:
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
            if name == _META_FILE:
                continue
            full = Path(root) / name
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
    raw = _read_text_guarded(resolved, path)
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
    raw = _read_text_guarded(resolved, path)
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
    raw = _read_text_guarded(resolved, path)
    stat = resolved.stat()
    return FileResponse(
        path=path,
        content=raw.decode("utf-8", errors="replace"),
        size=stat.st_size,
        mtime=stat.st_mtime,
    )


# ── workspace management endpoints (/workspaces) ─────────────────────────────

_META_FILE = ".macae_workspace_meta.json"


def _user_root(request: Request) -> Path:
    """Return the {WORKSPACE_ROOT}/{user_id}/ directory (never creates it)."""
    user_id = _auth_user(request)
    if not _SAFE_ID.match(user_id):
        raise HTTPException(status_code=400, detail="Invalid user id.")
    return _contained(WORKSPACE_ROOT, user_id)


def _read_meta(ws: Path) -> dict:
    meta_path = ws / _META_FILE
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write_meta(ws: Path, meta: dict) -> None:
    (ws / _META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _count_files(ws: Path) -> int:
    count = 0
    for _root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d != ".git"]
        count += sum(1 for f in files if f != _META_FILE)
    return count


class WorkspaceSummary(BaseModel):
    workspace_id: str
    name: str
    created_at: str  # ISO-8601
    file_count: int


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceSummary]


class WorkspaceCreateRequest(BaseModel):
    name: str
    workspace_id: str | None = None  # client-supplied slug; server generates if omitted


class WorkspaceCreateResponse(BaseModel):
    workspace_id: str
    name: str
    created_at: str
    file_count: int


@workspaces_router.get("", response_model=WorkspaceListResponse)
def list_workspaces(request: Request) -> WorkspaceListResponse:
    """List all workspaces owned by the authenticated user."""
    user_root = _user_root(request)
    results: list[WorkspaceSummary] = []
    if not user_root.exists():
        return WorkspaceListResponse(workspaces=[])
    for entry in sorted(user_root.iterdir()):
        if not entry.is_dir() or not (entry / ".git").is_dir():
            continue
        # Only NAMED workspaces (created via create_workspace, which writes the
        # meta file) belong in the selector; auto-created per-session spaces
        # are reachable through the session fallback, never listed here.
        if not (entry / _META_FILE).exists():
            continue
        meta = _read_meta(entry)
        results.append(
            WorkspaceSummary(
                workspace_id=entry.name,
                name=meta.get("name", entry.name),
                created_at=meta.get(
                    "created_at",
                    datetime.fromtimestamp(
                        entry.stat().st_ctime, tz=timezone.utc
                    ).isoformat(),
                ),
                file_count=_count_files(entry),
            )
        )
    return WorkspaceListResponse(workspaces=results)


@workspaces_router.post("", response_model=WorkspaceCreateResponse, status_code=201)
def create_workspace(
    request: Request, body: WorkspaceCreateRequest
) -> WorkspaceCreateResponse:
    """Create (or re-open) a named workspace."""
    user_id = _auth_user(request)
    if not _SAFE_ID.match(user_id):
        raise HTTPException(status_code=400, detail="Invalid user id.")

    # Derive workspace_id from supplied name if not given
    raw_id = (body.workspace_id or body.name).strip()
    # Slugify: lowercase, replace non-alnum runs with '-', trim dashes
    slug = re.sub(r"[^A-Za-z0-9]+", "-", raw_id.lower()).strip("-")
    if not slug:
        slug = "workspace"
    workspace_id = slug[:64]

    if not _SAFE_ID.match(workspace_id):
        raise HTTPException(status_code=400, detail="Invalid workspace id.")

    ws = _contained(WORKSPACE_ROOT, user_id, workspace_id)
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    if not (ws / ".git").is_dir():
        with _init_lock:
            if not (ws / ".git").is_dir():
                ws.mkdir(parents=True, exist_ok=True)
                # Exclude meta file from the user's git history
                (ws / ".gitignore").write_text(_META_FILE + "\n", encoding="utf-8")
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
                meta = {"name": body.name.strip(), "created_at": now_iso}
                _write_meta(ws, meta)
    else:
        # Update name if workspace already exists
        meta = _read_meta(ws)
        meta["name"] = body.name.strip()
        _write_meta(ws, meta)
        now_iso = meta.get("created_at", now_iso)

    return WorkspaceCreateResponse(
        workspace_id=workspace_id,
        name=body.name.strip(),
        created_at=now_iso,
        file_count=_count_files(ws),
    )


@workspaces_router.delete(
    "/{workspace_id}",
    status_code=204,
    response_model=None,
    response_class=Response,
)
def delete_workspace(request: Request, workspace_id: str) -> None:
    """Delete a workspace and all its files. Irreversible."""
    import shutil

    user_id = _auth_user(request)
    if not _SAFE_ID.match(user_id):
        raise HTTPException(status_code=400, detail="Invalid user id.")
    if not _SAFE_ID.match(workspace_id):
        raise HTTPException(status_code=400, detail="Invalid workspace id.")
    ws = _contained(WORKSPACE_ROOT, user_id, workspace_id)
    if not ws.exists():
        raise HTTPException(status_code=404, detail="Workspace not found.")
    shutil.rmtree(ws)
    return None
