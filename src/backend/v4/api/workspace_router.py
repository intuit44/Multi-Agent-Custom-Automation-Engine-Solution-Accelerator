"""
Workspace file-editing endpoints — dev-only by default.

Exposes the same physical workspace that mcp-server-filesystem uses,
so Monaco (browser) and MCP agents always read/write the same source of truth.

Endpoints
---------
GET  /workspace/files/{path}   → { path, content, size, mtime }
PUT  /workspace/files/{path}   → { path, size, mtime }
GET  /workspace/diff/{path}    → { path, original, modified } (git HEAD vs disk)

Security
--------
- Only active when config.APP_ENV == "dev"  (prod returns 403 immediately).
- Path is resolved relative to WORKSPACE_ROOT and checked with
  Path.is_relative_to() so ".." traversal is impossible.
- Binary files (detected by null bytes) are rejected with 415.
- Files > MAX_FILE_BYTES (1 MB) refused for PUT; GET returns download URL hint.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from common.config.app_config import config

workspace_router = APIRouter(prefix="/workspace", tags=["workspace"])

# ── constants ────────────────────────────────────────────────────────────────
WORKSPACE_ROOT = Path(
    "/workspaces/Multi-Agent-Custom-Automation-Engine-Solution-Accelerator"
)
MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MB


# ── helpers ──────────────────────────────────────────────────────────────────


def _dev_only() -> None:
    if config.APP_ENV != "dev":
        raise HTTPException(
            status_code=403,
            detail="Workspace file editing is only available in dev mode.",
        )


def _resolve(raw: str) -> Path:
    """Resolve *raw* relative to WORKSPACE_ROOT; reject traversal."""
    # FastAPI path params strip leading slash — re-add so Path resolves correctly.
    candidate = (WORKSPACE_ROOT / raw.lstrip("/")).resolve()
    try:
        candidate.relative_to(WORKSPACE_ROOT)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path outside workspace.")
    return candidate


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _git_show(rel: str) -> str | None:
    """Return HEAD content of *rel* (workspace-relative), or None if not tracked."""
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            cwd=WORKSPACE_ROOT,
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace")
        return None
    except Exception:
        return None


# ── models ───────────────────────────────────────────────────────────────────


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


# ── endpoints ────────────────────────────────────────────────────────────────


@workspace_router.get("/files/{path:path}", response_model=FileResponse)
async def get_file(path: str) -> FileResponse:
    """Read a workspace file as text."""
    _dev_only()
    resolved = _resolve(path)
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
    stat = resolved.stat()
    return FileResponse(
        path=path,
        content=raw.decode("utf-8", errors="replace"),
        size=stat.st_size,
        mtime=stat.st_mtime,
    )


@workspace_router.put("/files/{path:path}", response_model=FilePutResponse)
async def put_file(path: str, body: FilePutRequest) -> FilePutResponse:
    """Overwrite a workspace file from Monaco."""
    _dev_only()
    resolved = _resolve(path)
    if not resolved.parent.exists():
        raise HTTPException(status_code=400, detail="Parent directory does not exist.")
    encoded = body.content.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Content exceeds {MAX_FILE_BYTES // 1024} KB limit.",
        )
    resolved.write_bytes(encoded)
    stat = resolved.stat()
    return FilePutResponse(path=path, size=stat.st_size, mtime=stat.st_mtime)


@workspace_router.get("/diff/{path:path}", response_model=DiffResponse)
async def get_diff(path: str) -> DiffResponse:
    """Return git HEAD vs disk for Monaco DiffEditor."""
    _dev_only()
    resolved = _resolve(path)
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    raw = resolved.read_bytes()
    if _is_binary(raw):
        raise HTTPException(status_code=415, detail="Binary file — no diff available.")
    disk_content = raw.decode("utf-8", errors="replace")
    rel = str(resolved.relative_to(WORKSPACE_ROOT))
    head_content = _git_show(rel) or ""
    return DiffResponse(path=path, original=head_content, modified=disk_content)
