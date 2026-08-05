#!/usr/bin/env python3
"""
scripts/sync_agent_teams.py
============================
Idempotent upsert of all data/agent_teams/*.json files into Cosmos DB.

WHY THIS EXISTS
---------------
The data/agent_teams/*.json files are the source of truth for agent
configuration (system_message, tools, model, etc.).  At runtime the
backend reads agent config from Cosmos DB, NOT directly from the JSON.

If the JSON and Cosmos diverge, you get hard-to-debug inconsistencies:
  • JSON: use_mcp=false  →  Cosmos: use_mcp=true  (MCP mode in runtime)
  • JSON: use_reasoning=true + coding_tools=true  →  Cosmos: use_reasoning=false
    (avoids InvalidConfigurationError in factory but silently drops RAG)

This script re-syncs Cosmos from the JSON files whenever you edit them.

WHEN TO RUN
-----------
  • After editing any data/agent_teams/*.json
  • After cloning the repo on a new environment
  • After a DB wipe / fresh Cosmos setup
  • In CI before integration tests

USAGE
-----
  # From repo root:
  cd src/backend
  uv run python ../../scripts/sync_agent_teams.py

  # Dry-run (shows what would change, does not write):
  uv run python ../../scripts/sync_agent_teams.py --dry-run

  # Specific file only:
  uv run python ../../scripts/sync_agent_teams.py --file hr.json

  # Verbose diff output:
  uv run python ../../scripts/sync_agent_teams.py --verbose

REQUIRED ENV VARS (same as the backend):
  COSMOSDB_ENDPOINT   — Cosmos DB account URI
  COSMOSDB_DATABASE   — database name
  COSMOSDB_CONTAINER  — container name (partition key: /session_id)
  AZURE_CLIENT_ID     — optional: service principal client ID
  AZURE_TENANT_ID     — optional: service principal tenant ID
  AZURE_CLIENT_SECRET — optional: service principal secret
  (If SP vars absent, falls back to DefaultAzureCredential / CLI credential)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Path setup ─────────────────────────────────────────────────────────────
# When run as  `uv run python ../../scripts/sync_agent_teams.py`
# from src/backend/, the backend package is on sys.path.
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "src" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

AGENT_TEAMS_DIR = REPO_ROOT / "data" / "agent_teams"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sync_agent_teams")


# ── Cosmos helpers (sync, no dependency on the full backend ORM) ────────────


def _get_cosmos_container():
    """Return a synchronous Cosmos ContainerProxy using env-var credentials."""
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    endpoint = os.environ.get("COSMOSDB_ENDPOINT") or _fail("COSMOSDB_ENDPOINT")
    database = os.environ.get("COSMOSDB_DATABASE") or _fail("COSMOSDB_DATABASE")
    container = os.environ.get("COSMOSDB_CONTAINER") or _fail("COSMOSDB_CONTAINER")

    # Prefer service-principal creds if all three vars are present
    client_id = os.environ.get("AZURE_CLIENT_ID")
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET")

    if client_id and tenant_id and client_secret:
        from azure.identity import ClientSecretCredential

        cred = ClientSecretCredential(tenant_id, client_id, client_secret)
        logger.debug("Using ClientSecretCredential")
    else:
        cred = DefaultAzureCredential()
        logger.debug("Using DefaultAzureCredential")

    cosmos = CosmosClient(url=endpoint, credential=cred)
    return cosmos.get_database_client(database).get_container_client(container)


def _fail(var: str) -> str:
    logger.error("Missing required environment variable: %s", var)
    sys.exit(1)


# ── JSON → Cosmos document ──────────────────────────────────────────────────

# Two lookup maps for built-in teams (matches utils_af.py priority list):
#   hr=001, marketing=002, retail=003, rfp=004, contract_compliance=005
#
# _SYSTEM_TEAM_IDS_BY_NAME  — keyed by filename stem (primary lookup)
# _SYSTEM_TEAM_IDS_BY_NUMBER — keyed by the legacy numeric string id field
#   so that a JSON file with "id": "1" still resolves to the correct UUID
#   even though the name-based map no longer holds "1".."5" keys.
_SYSTEM_TEAM_IDS_BY_NAME: dict[str, str] = {
    "hr": "00000000-0000-0000-0000-000000000001",
    "marketing": "00000000-0000-0000-0000-000000000002",
    "retail": "00000000-0000-0000-0000-000000000003",
    "rfp_analysis_team": "00000000-0000-0000-0000-000000000004",
    "contract_compliance_team": "00000000-0000-0000-0000-000000000005",
}
_SYSTEM_TEAM_IDS_BY_NUMBER: dict[str, str] = {
    "1": "00000000-0000-0000-0000-000000000001",
    "2": "00000000-0000-0000-0000-000000000002",
    "3": "00000000-0000-0000-0000-000000000003",
    "4": "00000000-0000-0000-0000-000000000004",
    "5": "00000000-0000-0000-0000-000000000005",
}


def _json_to_document(raw: dict[str, Any], source_file: str) -> dict[str, Any]:
    """
    Convert a raw agent_teams JSON dict into a Cosmos document.

    Rules
    -----
    • team_id: if raw["id"] is a single digit ("1".."5") we map it to the
      well-known UUID used by the bootstrap logic (utils_af.py).
      Otherwise the raw value is used verbatim.
    • id (document PK):  same as team_id — ensures upsert is idempotent.
    • session_id (partition key): stable UUID derived from team_id so
      re-runs do not create orphan partitions.
    • data_type: always "team_config"
    • user_id: "system" — marks these as built-in teams
    • created / created_by: filled if missing
    """
    file_stem = Path(source_file).stem
    raw_id = str(raw.get("id", ""))
    # 1. filename stem (e.g. "hr", "retail")
    # 2. legacy numeric id field ("1".."5") via the number map
    # 3. raw value verbatim — for genuinely custom teams that already carry
    #    a proper UUID or slug as their id.
    team_id = (
        _SYSTEM_TEAM_IDS_BY_NAME.get(file_stem)
        or _SYSTEM_TEAM_IDS_BY_NUMBER.get(raw_id)
        or raw_id
    )

    # Stable partition key: UUID v5(namespace_dns, team_id) so it is
    # deterministic across runs.
    session_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"team:{team_id}"))

    now = datetime.now(timezone.utc).isoformat()

    agents: list[dict] = []
    for a in raw.get("agents", []):
        agents.append(
            {
                "input_key": a.get("input_key", ""),
                "type": a.get("type", ""),
                "name": a.get("name", ""),
                "deployment_name": a.get("deployment_name", ""),
                "icon": a.get("icon", ""),
                "system_message": a.get("system_message", ""),
                "description": a.get("description", ""),
                "use_rag": bool(a.get("use_rag", False)),
                "use_mcp": bool(a.get("use_mcp", False)),
                "use_bing": bool(a.get("use_bing", False)),
                "use_reasoning": bool(a.get("use_reasoning", False)),
                "index_name": a.get("index_name", ""),
                "index_foundry_name": a.get("index_foundry_name", ""),
                "index_endpoint": a.get("index_endpoint", ""),
                "coding_tools": bool(a.get("coding_tools", False)),
            }
        )

    tasks: list[dict] = []
    for t in raw.get("starting_tasks", []):
        # Deterministic fallback: UUID v5 from team_id + task name + prompt
        # so repeated runs do not generate new UUIDs and cause spurious diffs.
        task_id_fallback = str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"task:{team_id}:{t.get('name', '')}:{t.get('prompt', '')}",
            )
        )
        tasks.append(
            {
                "id": t.get("id") or task_id_fallback,
                "name": t.get("name", ""),
                "prompt": t.get("prompt", ""),
                "created": t.get("created", ""),
                "creator": t.get("creator", ""),
                "logo": t.get("logo", ""),
            }
        )

    return {
        # Cosmos document identity
        "id": team_id,  # document id
        "session_id": session_id,  # partition key
        "team_id": team_id,
        "data_type": "team_config",
        "user_id": "system",
        # Team metadata
        "name": raw.get("name", ""),
        "status": raw.get("status", "visible"),
        "description": raw.get("description", ""),
        "logo": raw.get("logo", ""),
        "plan": raw.get("plan", ""),
        "deployment_name": raw.get("deployment_name", ""),
        "protected": bool(raw.get("protected", False)),
        "created": raw.get("created") or now,
        "created_by": raw.get("created_by") or "sync_agent_teams",
        # Children
        "agents": agents,
        "starting_tasks": tasks,
        # Audit
        "_synced_from": source_file,
        "_synced_at": now,
    }


# ── Diff helpers ─────────────────────────────────────────────────────────────

_SKIP_AUDIT = {
    "_synced_from",
    "_synced_at",
    "session_id",
    "_ts",
    "_rid",
    "_self",
    "_etag",
    "_attachments",
}


def _diff(old: dict, new: dict) -> list[str]:
    """Return human-readable lines describing field-level changes."""
    lines: list[str] = []
    all_keys = set(old) | set(new)
    for k in sorted(all_keys):
        if k in _SKIP_AUDIT:
            continue
        ov, nv = old.get(k, "<absent>"), new.get(k, "<absent>")
        if ov != nv:
            if isinstance(ov, str) and len(ov) > 80:
                ov = ov[:77] + "..."
            if isinstance(nv, str) and len(nv) > 80:
                nv = nv[:77] + "..."
            lines.append(f"  {k}:\n    was: {ov!r}\n    now: {nv!r}")
    return lines


# ── Core sync logic ──────────────────────────────────────────────────────────


def _load_existing(container, team_id: str) -> dict[str, Any] | None:
    """Fetch the current Cosmos document for *team_id*, or None."""
    try:
        items = list(
            container.query_items(
                query="SELECT * FROM c WHERE c.team_id=@id AND c.data_type='team_config'",
                parameters=[{"name": "@id", "value": team_id}],
                enable_cross_partition_query=True,
            )
        )
        return items[0] if items else None
    except Exception as exc:
        logger.warning("Could not query Cosmos for team_id=%s: %s", team_id, exc)
        return None


def sync_file(
    path: Path,
    container,
    *,
    dry_run: bool = False,
    verbose: bool = False,
) -> str:
    """
    Upsert one JSON file into Cosmos.

    Returns one of: "created" | "updated" | "unchanged" | "error"
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Cannot parse %s: %s", path.name, exc)
        return "error"

    try:
        doc = _json_to_document(raw, path.name)
    except Exception as exc:
        logger.error("Cannot convert %s to Cosmos document: %s", path.name, exc)
        return "error"

    team_id = doc["team_id"]
    existing = _load_existing(container, team_id)

    if existing is not None:
        delta = _diff(existing, doc)
        if not delta:
            logger.info("%-40s  ✓ unchanged  (team_id=%s)", path.name, team_id)
            return "unchanged"

        if verbose:
            logger.info("%-40s  ~ CHANGES  (team_id=%s)", path.name, team_id)
            for line in delta:
                print(line)
        else:
            logger.info(
                "%-40s  ~ updated  (team_id=%s, %d field(s) changed)",
                path.name,
                team_id,
                len(delta),
            )

        if not dry_run:
            # Preserve the original Cosmos session_id / partition key so we
            # can upsert into the same partition without a cross-partition move.
            doc["session_id"] = existing.get("session_id", doc["session_id"])
            doc["id"] = existing.get("id", doc["id"])
            # Preserve the original creation timestamp — it must never change
            # after first write; otherwise every run shows a spurious diff.
            if existing.get("created"):
                doc["created"] = existing["created"]
            # Preserve per-task created timestamps: match by task id so that
            # re-syncing does not overwrite the original creation time of each
            # task and avoids spurious diffs on the starting_tasks list.
            existing_tasks_by_id: dict[str, str] = {
                t["id"]: t["created"]
                for t in existing.get("starting_tasks", [])
                if t.get("id") and t.get("created")
            }
            for task in doc.get("starting_tasks", []):
                old_created = existing_tasks_by_id.get(task["id"])
                if old_created:
                    task["created"] = old_created
            container.upsert_item(body=doc)
        return "updated"

    else:
        logger.info("%-40s  + created  (team_id=%s)", path.name, team_id)
        if not dry_run:
            container.upsert_item(body=doc)
        return "created"


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Idempotent upsert of data/agent_teams/*.json into Cosmos DB",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to Cosmos",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show field-level diffs for changed documents",
    )
    parser.add_argument(
        "--file",
        metavar="FILENAME",
        help="Sync only this filename (e.g. hr.json).  Default: all files.",
    )
    parser.add_argument(
        "--teams-dir",
        metavar="DIR",
        default=str(AGENT_TEAMS_DIR),
        help=f"Directory containing agent_teams JSON files (default: {AGENT_TEAMS_DIR})",
    )
    args = parser.parse_args()

    teams_dir = Path(args.teams_dir)
    if not teams_dir.is_dir():
        logger.error("agent_teams directory not found: %s", teams_dir)
        sys.exit(1)

    if args.file:
        files = [teams_dir / args.file]
        if not files[0].exists():
            logger.error("File not found: %s", files[0])
            sys.exit(1)
    else:
        files = sorted(teams_dir.glob("*.json"))

    if not files:
        logger.warning("No JSON files found in %s", teams_dir)
        sys.exit(0)

    # Load .env if present (for local dev convenience)
    try:
        from dotenv import load_dotenv

        env_path = BACKEND_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            logger.debug("Loaded .env from %s", env_path)
    except ImportError:
        pass

    if args.dry_run:
        logger.info("DRY-RUN mode — no writes will be made")

    container = _get_cosmos_container()

    counts: dict[str, int] = {"created": 0, "updated": 0, "unchanged": 0, "error": 0}
    for f in files:
        result = sync_file(f, container, dry_run=args.dry_run, verbose=args.verbose)
        counts[result] += 1

    # Summary
    total = len(files)
    logger.info(
        "Done — %d file(s): %d created, %d updated, %d unchanged, %d error(s)",
        total,
        counts["created"],
        counts["updated"],
        counts["unchanged"],
        counts["error"],
    )
    if counts["error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
