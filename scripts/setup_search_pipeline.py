#!/usr/bin/env python3
"""
Setup AI Search pipeline: vector + semantic search for MACAE.

Usage:
    cd src/backend && .venv/bin/python ../../scripts/setup_search_pipeline.py

Prereqs (already done):
    ✅ text-embedding-3-small deployed in Azure OpenAI
    ✅ AI Search service running (basic tier, semanticSearch: "free")
    ✅ Cosmos DB with chat_sessions container

Steps executed:
    0. Migrate 10 doc indices to canonical schema (id filterable, vectorizer,
       source_blob, indexed_at). Idempotente: skip si ya canónico, crea si no
       existe, o dump→DELETE→recreate→re-upload si está desalineado.
    1. Upgrade 8 existing indices → add content_vector + vectorSearch + semantic config
       (legacy, casi no-op tras Step 0).
    2. Create or upgrade chat-history-index (idempotente; añade vectorizer si falta).
    3. Backfill embeddings for documents sin content_vector.
    4. Backfill all chat messages from Cosmos DB → chat-history-index.
    5. Create Cosmos datasource + embedding skillset + continuous indexer (PT5M schedule)
       Self-healing layer for the push fire-and-forget path in chat_cosmos_service.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import aiohttp
from azure.identity.aio import DefaultAzureCredential as DefaultAzureCredentialAsync

# ── Load env ─────────────────────────────────────────────────────
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(SCRIPT_DIR, "..", "src", "backend")
load_dotenv(os.path.join(BACKEND_DIR, ".env"), override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("setup_search")

# ── Config ───────────────────────────────────────────────────────

SEARCH_ENDPOINT = os.getenv("AZURE_AI_SEARCH_ENDPOINT", "").rstrip("/")
OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
EMBEDDING_DEPLOYMENT = os.getenv(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
)
EMBEDDING_DIMS = 1536
COSMOS_ENDPOINT = os.getenv("COSMOSDB_ENDPOINT", "")
COSMOS_DATABASE = os.getenv("COSMOSDB_DATABASE", "macae")
COSMOS_CONNECTION_STRING = os.getenv("COSMOSDB_CONNECTION_STRING", "")
COSMOS_CHAT_CONTAINER = "chat_sessions"

SEARCH_API = "2024-07-01"
OPENAI_API = "2024-10-21"

# Indexer continuo
CHAT_DATASOURCE_NAME = "chat-sessions-cosmos-ds"
CHAT_SKILLSET_NAME = "chat-history-skillset"
CHAT_INDEXER_NAME = "chat-history-indexer"
CHAT_INDEXER_SCHEDULE_INTERVAL = "PT5M"  # cada 5 minutos

EXISTING_INDICES = [
    "contract-compliance-doc-index",
    "contract-risk-doc-index",
    "contract-summary-doc-index",
    "macae-retail-customer-index",
    "macae-retail-order-index",
    "macae-rfp-compliance-index",
    "macae-rfp-risk-index",
    "macae-rfp-summary-index",
]

# Índices nuevos a crear si no existen (HR / Marketing — antes huérfanos)
NEW_DOC_INDICES = [
    "macae-hr-knowledge-index",
    "macae-marketing-knowledge-index",
]

# ── Schema templates ─────────────────────────────────────────────

VECTOR_SEARCH_CONFIG = {
    "algorithms": [
        {
            "name": "hnsw-algo",
            "kind": "hnsw",
            "hnswParameters": {
                "m": 4,
                "efConstruction": 400,
                "efSearch": 500,
                "metric": "cosine",
            },
        }
    ],
    "profiles": [
        {
            "name": "vector-profile",
            "algorithm": "hnsw-algo",
            "vectorizer": "aoai-vectorizer",
        }
    ],
    "vectorizers": [
        {
            "name": "aoai-vectorizer",
            "kind": "azureOpenAI",
            "azureOpenAIParameters": {
                "resourceUri": OPENAI_ENDPOINT,
                "deploymentId": EMBEDDING_DEPLOYMENT,
                "modelName": EMBEDDING_DEPLOYMENT,
            },
        }
    ],
}

CONTENT_VECTOR_FIELD = {
    "name": "content_vector",
    "type": "Collection(Edm.Single)",
    "searchable": True,
    "retrievable": False,
    "stored": True,
    "dimensions": EMBEDDING_DIMS,
    "vectorSearchProfile": "vector-profile",
}

DOC_SEMANTIC_CONFIG = {
    "configurations": [
        {
            "name": "default",
            "prioritizedFields": {
                "titleField": {"fieldName": "title"},
                "prioritizedContentFields": [{"fieldName": "content"}],
                "prioritizedKeywordsFields": [],
            },
        }
    ],
}

CHAT_SEMANTIC_CONFIG = {
    "configurations": [
        {
            "name": "chat-semantic-config",
            "prioritizedFields": {
                "titleField": {"fieldName": "title"},
                "prioritizedContentFields": [{"fieldName": "content"}],
                "prioritizedKeywordsFields": [
                    {"fieldName": "session_name"},
                    {"fieldName": "role"},
                    {"fieldName": "intent"},
                ],
            },
        }
    ],
}


def build_doc_index_schema(name: str) -> dict:
    """Schema canónico para los 10 índices de documentos.

    Campos: id (filterable), title, content, source_blob, indexed_at,
    content_vector. vectorSearch HNSW + Azure OpenAI vectorizer.
    semantic config 'default' (titleField=title, content=[content]).
    """
    return {
        "name": name,
        "fields": [
            {
                "name": "id",
                "type": "Edm.String",
                "key": True,
                "searchable": False,
                "filterable": True,
                "retrievable": True,
            },
            {
                "name": "title",
                "type": "Edm.String",
                "searchable": True,
                "filterable": True,
                "retrievable": True,
            },
            {
                "name": "content",
                "type": "Edm.String",
                "searchable": True,
                "filterable": False,
                "retrievable": True,
            },
            {
                "name": "source_blob",
                "type": "Edm.String",
                "searchable": False,
                "filterable": True,
                "retrievable": True,
            },
            {
                "name": "indexed_at",
                "type": "Edm.DateTimeOffset",
                "searchable": False,
                "filterable": True,
                "sortable": True,
                "retrievable": True,
            },
            {
                "name": "content_vector",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "stored": True,
                "dimensions": EMBEDDING_DIMS,
                "vectorSearchProfile": "vector-profile",
            },
        ],
        "vectorSearch": VECTOR_SEARCH_CONFIG,
        "semantic": DOC_SEMANTIC_CONFIG,
    }


CHAT_HISTORY_INDEX_SCHEMA = {
    "name": "chat-history-index",
    "fields": [
        {
            "name": "id",
            "type": "Edm.String",
            "key": True,
            "searchable": False,
            "filterable": True,
            "retrievable": True,
        },
        {
            "name": "session_id",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "retrievable": True,
        },
        {
            "name": "user_id",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "retrievable": True,
        },
        {
            "name": "role",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "retrievable": True,
        },
        {
            "name": "title",
            "type": "Edm.String",
            "searchable": True,
            "filterable": False,
            "retrievable": True,
        },
        {
            "name": "content",
            "type": "Edm.String",
            "searchable": True,
            "filterable": False,
            "retrievable": True,
        },
        {
            "name": "content_vector",
            "type": "Collection(Edm.Single)",
            "searchable": True,
            "retrievable": False,
            "stored": True,
            "dimensions": EMBEDDING_DIMS,
            "vectorSearchProfile": "vector-profile",
        },
        {
            "name": "intent",
            "type": "Edm.String",
            "searchable": False,
            "filterable": True,
            "retrievable": True,
        },
        {
            "name": "timestamp",
            "type": "Edm.DateTimeOffset",
            "searchable": False,
            "filterable": True,
            "sortable": True,
            "retrievable": True,
        },
        {
            "name": "session_name",
            "type": "Edm.String",
            "searchable": True,
            "filterable": False,
            "retrievable": True,
        },
    ],
    "vectorSearch": VECTOR_SEARCH_CONFIG,
    "semantic": CHAT_SEMANTIC_CONFIG,
}


def build_chat_title(session_name: str, role: str, intent: str, content: str) -> str:
    """Build a compact semantic title for chat-history documents."""
    base = (session_name or "").strip() or "Chat session"
    role_label = (role or "message").strip()
    intent_label = (intent or "").strip()
    snippet = " ".join((content or "").split())[:80]

    parts = [base, role_label]
    if intent_label:
        parts.append(intent_label)
    if snippet:
        parts.append(snippet)
    return " | ".join(parts)


# ── Azure API client ─────────────────────────────────────────────


class AzureClients:
    """Thin wrapper for Azure REST API calls."""

    def __init__(self):
        self.credential = DefaultAzureCredentialAsync()
        self._session: Optional[aiohttp.ClientSession] = None

    async def init_session(self):
        self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session:
            await self._session.close()
        await self.credential.close()

    async def _search_headers(self) -> dict:
        token = await self.credential.get_token("https://search.azure.com/.default")
        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

    async def _openai_headers(self) -> dict:
        token = await self.credential.get_token(
            "https://cognitiveservices.azure.com/.default"
        )
        return {
            "Authorization": f"Bearer {token.token}",
            "Content-Type": "application/json",
        }

    # ── Search index operations ──────────────────────────────────

    async def get_index(self, name: str) -> Optional[dict]:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexes/{name}?api-version={SEARCH_API}"
        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

    async def put_index(self, name: str, schema: dict) -> bool:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexes/{name}?api-version={SEARCH_API}"
        async with self._session.put(url, headers=headers, json=schema) as resp:
            if resp.status in (200, 201, 204):
                return True
            body = await resp.text()
            logger.error(
                "PUT index %s failed: HTTP %s — %s", name, resp.status, body[:500]
            )
            return False

    async def create_index(self, schema: dict) -> bool:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexes?api-version={SEARCH_API}"
        async with self._session.post(url, headers=headers, json=schema) as resp:
            if resp.status in (200, 201):
                return True
            body = await resp.text()
            logger.error("Create index failed: HTTP %s — %s", resp.status, body[:500])
            return False

    async def delete_index(self, name: str) -> bool:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexes/{name}?api-version={SEARCH_API}"
        async with self._session.delete(url, headers=headers) as resp:
            if resp.status in (200, 204, 404):
                return True
            body = await resp.text()
            logger.error(
                "DELETE index %s failed: HTTP %s — %s", name, resp.status, body[:500]
            )
            return False

    async def search_docs(
        self, index_name: str, search: str = "*", top: int = 1000
    ) -> list:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexes/{index_name}/docs/search?api-version={SEARCH_API}"
        body = {"search": search, "top": top, "select": "*"}
        async with self._session.post(url, headers=headers, json=body) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("value", [])
            return []

    async def upload_docs(self, index_name: str, docs: list) -> bool:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexes/{index_name}/docs/index?api-version={SEARCH_API}"
        async with self._session.post(
            url, headers=headers, json={"value": docs}
        ) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                failed = [r for r in data.get("value", []) if not r.get("status")]
                if failed:
                    logger.warning("  Some docs failed: %s", failed[:3])
                return True
            body = await resp.text()
            logger.error(
                "Upload to %s failed: HTTP %s — %s",
                index_name,
                resp.status,
                body[:500],
            )
            return False

    # ── Datasource / Skillset / Indexer (continuous ingestion) ────

    async def put_resource(self, kind: str, name: str, body: dict) -> bool:
        """PUT a Search resource (datasources, skillsets, indexers).

        kind ∈ {'datasources', 'skillsets', 'indexers'}
        """
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/{kind}/{name}?api-version={SEARCH_API}"
        async with self._session.put(url, headers=headers, json=body) as resp:
            if resp.status in (200, 201, 204):
                return True
            text = await resp.text()
            logger.error(
                "PUT %s/%s failed: HTTP %s — %s", kind, name, resp.status, text[:500]
            )
            return False

    async def get_resource(self, kind: str, name: str) -> Optional[dict]:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/{kind}/{name}?api-version={SEARCH_API}"
        async with self._session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            return None

    async def run_indexer(self, name: str) -> bool:
        headers = await self._search_headers()
        url = f"{SEARCH_ENDPOINT}/indexers/{name}/run?api-version={SEARCH_API}"
        async with self._session.post(url, headers=headers) as resp:
            return resp.status in (202, 204)

    # ── Embedding generation ─────────────────────────────────────

    async def generate_embeddings(
        self, texts: List[str]
    ) -> Optional[List[List[float]]]:
        """Batch embedding generation (chunks of 16 to avoid token limits)."""
        if not texts:
            return []

        all_embeddings: List[List[float]] = []
        url = (
            f"{OPENAI_ENDPOINT}/openai/deployments/{EMBEDDING_DEPLOYMENT}"
            f"/embeddings?api-version={OPENAI_API}"
        )

        for i in range(0, len(texts), 16):
            batch = [t[:8000] for t in texts[i : i + 16]]
            headers = await self._openai_headers()
            async with self._session.post(
                url, headers=headers, json={"input": batch}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    all_embeddings.extend([d["embedding"] for d in data["data"]])
                else:
                    body = await resp.text()
                    logger.error(
                        "Embedding error (batch %d): HTTP %s — %s",
                        i,
                        resp.status,
                        body[:300],
                    )
                    return None
            if i + 16 < len(texts):
                await asyncio.sleep(0.5)

        return all_embeddings


# ═══════════════════════════════════════════════════════════════════
# Step 0: Migrate doc indices to canonical schema
# ─── Recorre los 10 índices de documentos. Si el índice no existe,
# ─── lo crea con schema canónico. Si existe pero no cumple (id no
# ─── filterable, sin vectorizer, sin source_blob/indexed_at, etc.),
# ─── dump → DELETE → recreate canónico → re-upload (sin vector — los
# ─── vectores se regeneran en Step 3). Si ya cumple, skip. Idempotente.
# ═══════════════════════════════════════════════════════════════════


def _doc_index_is_canonical(schema: dict) -> bool:
    """Verifica si un schema cumple el contrato canónico actual."""
    fields_by_name = {f["name"]: f for f in schema.get("fields", [])}

    # 1. id presente, key y filterable
    id_field = fields_by_name.get("id")
    if not id_field or not id_field.get("key") or not id_field.get("filterable"):
        return False

    # 2. campos canónicos presentes
    for required in ("title", "content", "source_blob", "indexed_at", "content_vector"):
        if required not in fields_by_name:
            return False

    # 3. vectorSearch con vectorizer + profile que lo referencia
    vs = schema.get("vectorSearch") or {}
    vectorizers = vs.get("vectorizers") or []
    profiles = vs.get("profiles") or []
    if not vectorizers or not any(p.get("vectorizer") for p in profiles):
        return False

    # 4. semantic config con titleField + contentFields=[content]
    sem = schema.get("semantic") or {}
    if not sem.get("configurations"):
        return False

    return True


async def migrate_doc_indices_to_canonical(clients: AzureClients) -> int:
    """Lleva los 10 doc indices al schema canónico. Idempotente."""
    target_indices = list(EXISTING_INDICES) + list(NEW_DOC_INDICES)
    migrated = 0

    for idx_name in target_indices:
        logger.info("  Checking: %s", idx_name)
        existing = await clients.get_index(idx_name)

        # Caso A: no existe → crear con schema canónico
        if not existing:
            schema = build_doc_index_schema(idx_name)
            if await clients.create_index(schema):
                logger.info("    ✅ Created (canonical schema, empty)")
                migrated += 1
            else:
                logger.error("    ❌ Failed to create %s", idx_name)
            continue

        # Caso B: ya canónico → skip
        if _doc_index_is_canonical(existing):
            logger.info("    Already canonical ✅")
            migrated += 1
            continue

        # Caso C: existe pero no canónico → dump, delete, recreate, re-upload
        logger.info("    Not canonical — dumping docs before recreation...")
        docs = await clients.search_docs(idx_name)
        logger.info("    Dumped %d docs", len(docs))

        # Sanitizar: quitar @search.* y content_vector (se regenera en Step 3)
        sanitized: List[Dict[str, Any]] = []
        for d in docs:
            clean = {k: v for k, v in d.items() if not k.startswith("@") and k != "content_vector"}
            if "id" in clean:
                clean["@search.action"] = "mergeOrUpload"
                sanitized.append(clean)

        if not await clients.delete_index(idx_name):
            logger.error("    ❌ DELETE failed — abortando migración de %s", idx_name)
            continue

        schema = build_doc_index_schema(idx_name)
        if not await clients.create_index(schema):
            logger.error("    ❌ Recreate failed for %s", idx_name)
            continue

        if sanitized:
            if await clients.upload_docs(idx_name, sanitized):
                logger.info("    ✅ Migrated and re-uploaded %d docs (sin vector)", len(sanitized))
            else:
                logger.error("    ❌ Re-upload failed for %s", idx_name)
                continue
        else:
            logger.info("    ✅ Migrated (no docs to re-upload)")

        migrated += 1

    return migrated


# ═══════════════════════════════════════════════════════════════════
# Step 1: Upgrade existing indices
# ═══════════════════════════════════════════════════════════════════


async def upgrade_existing_indices(clients: AzureClients) -> int:
    """Add content_vector + vectorSearch + semantic config to existing indices."""
    upgraded = 0
    for idx_name in EXISTING_INDICES:
        logger.info("  Checking index: %s", idx_name)
        schema = await clients.get_index(idx_name)
        if not schema:
            logger.warning("    Index %s not found, skipping", idx_name)
            continue

        field_names = [f["name"] for f in schema.get("fields", [])]
        if "content_vector" in field_names:
            logger.info("    Already has content_vector ✅")
            upgraded += 1
            continue

        # Add vector field, vector search config, semantic config
        schema["fields"].append(CONTENT_VECTOR_FIELD)
        schema["vectorSearch"] = VECTOR_SEARCH_CONFIG
        schema["semantic"] = DOC_SEMANTIC_CONFIG

        # Remove OData metadata (can't send in PUT)
        for key in ["@odata.context", "@odata.etag"]:
            schema.pop(key, None)

        if await clients.put_index(idx_name, schema):
            logger.info("    ✅ Upgraded %s", idx_name)
            upgraded += 1
        else:
            logger.error("    ❌ Failed to upgrade %s", idx_name)

    return upgraded


# ═══════════════════════════════════════════════════════════════════
# Step 2: Create chat-history-index
# ═══════════════════════════════════════════════════════════════════


async def create_chat_history_index(clients: AzureClients) -> bool:
    """Create or upgrade chat-history-index. Idempotente.

    Fresh deploy → crea con vectorizer ya incluido (vía VECTOR_SEARCH_CONFIG).
    Migración → añade title field, vectorSearch.vectorizers + profile.vectorizer
    si faltan, y refresca semantic config si está desalineado. Mismo PUT.
    """
    existing = await clients.get_index("chat-history-index")
    if existing:
        logger.info("  chat-history-index already exists ✅")
        field_names = [f["name"] for f in existing.get("fields", [])]
        updated = False
        needs_semantic_update = existing.get("semantic") != CHAT_SEMANTIC_CONFIG

        if "title" not in field_names:
            existing["fields"].insert(
                4,
                {
                    "name": "title",
                    "type": "Edm.String",
                    "searchable": True,
                    "filterable": False,
                    "retrievable": True,
                },
            )
            updated = True

        # Vectorizer check: si vectorSearch no tiene vectorizers o el profile
        # no referencia uno, reemplazar el bloque entero por VECTOR_SEARCH_CONFIG
        # canónico. vectorSearch es config nivel-índice, se modifica vía PUT.
        vs = existing.get("vectorSearch") or {}
        if not vs.get("vectorizers") or not any(
            p.get("vectorizer") for p in (vs.get("profiles") or [])
        ):
            existing["vectorSearch"] = VECTOR_SEARCH_CONFIG
            updated = True

        if needs_semantic_update:
            existing["semantic"] = CHAT_SEMANTIC_CONFIG

        for key in ["@odata.context", "@odata.etag"]:
            existing.pop(key, None)

        if updated or needs_semantic_update:
            if await clients.put_index("chat-history-index", existing):
                logger.info("  ✅ Updated chat-history-index (title/vectorizer/semantic)")
                return True
            logger.error("  ❌ Failed to update chat-history-index")
            return False

        return True

    if await clients.create_index(CHAT_HISTORY_INDEX_SCHEMA):
        logger.info("  ✅ Created chat-history-index")
        return True

    logger.error("  ❌ Failed to create chat-history-index")
    return False


# ═══════════════════════════════════════════════════════════════════
# Step 3: Backfill embeddings for existing docs
# ═══════════════════════════════════════════════════════════════════


async def backfill_existing_docs(clients: AzureClients) -> int:
    """Generate and store embeddings for all existing documents."""
    total = 0
    for idx_name in EXISTING_INDICES:
        logger.info("  Backfilling: %s", idx_name)
        docs = await clients.search_docs(idx_name)
        if not docs:
            logger.info("    No documents found")
            continue

        # Check if first doc already has vector
        if docs[0].get("content_vector"):
            logger.info("    Already has vectors ✅ (%d docs)", len(docs))
            total += len(docs)
            continue

        contents = [d.get("content", "") or d.get("title", "") for d in docs]
        logger.info("    Generating embeddings for %d docs...", len(contents))
        embeddings = await clients.generate_embeddings(contents)
        if embeddings is None:
            logger.error("    Failed to generate embeddings")
            continue

        merge_docs = []
        for doc, vector in zip(docs, embeddings):
            merge_docs.append(
                {
                    "@search.action": "mergeOrUpload",
                    "id": doc["id"],
                    "content": doc.get("content", ""),
                    "title": doc.get("title", ""),
                    "content_vector": vector,
                }
            )

        if await clients.upload_docs(idx_name, merge_docs):
            logger.info("    ✅ %d docs embedded", len(merge_docs))
            total += len(merge_docs)
        else:
            logger.error("    ❌ Upload failed")

    return total


# ═══════════════════════════════════════════════════════════════════
# Step 4: Backfill chat history from Cosmos → chat-history-index
# ═══════════════════════════════════════════════════════════════════


async def backfill_chat_history(clients: AzureClients) -> int:
    """Index all existing chat messages from Cosmos DB."""
    from azure.cosmos.aio import CosmosClient

    if not COSMOS_ENDPOINT:
        logger.warning("  COSMOSDB_ENDPOINT not set, skipping")
        return 0

    cosmos = CosmosClient(url=COSMOS_ENDPOINT, credential=clients.credential)
    db = cosmos.get_database_client(COSMOS_DATABASE)
    container = db.get_container_client("chat_sessions")

    # Collect all messages from sessions
    all_messages: List[Dict[str, Any]] = []
    session_count = 0

    async for session in container.query_items(
        "SELECT c.id, c.user_id, c.session_name, c.messages FROM c "
        "WHERE ARRAY_LENGTH(c.messages) > 0"
    ):
        session_count += 1
        session_id = session["id"]
        user_id = session.get("user_id", "")
        session_name = session.get("session_name", "")

        for msg in session.get("messages", []):
            content = msg.get("content", "")
            if not content or len(content.strip()) < 3:
                continue
            all_messages.append(
                {
                    "message_id": msg.get("id", f"{session_id}-{len(all_messages)}"),
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": msg.get("role", "user"),
                    "content": content,
                    "intent": (msg.get("metadata") or {}).get("intent", ""),
                    "timestamp": msg.get("timestamp", ""),
                    "session_name": session_name,
                }
            )

    logger.info(
        "  Found %d messages across %d sessions", len(all_messages), session_count
    )

    if not all_messages:
        await cosmos.close()
        return 0

    # Generate embeddings
    contents = [m["content"] for m in all_messages]
    logger.info("  Generating embeddings for %d messages...", len(contents))
    embeddings = await clients.generate_embeddings(contents)
    if embeddings is None:
        logger.error("  Failed to generate embeddings")
        await cosmos.close()
        return 0

    # Build search docs
    search_docs: List[Dict[str, Any]] = []
    for msg, vector in zip(all_messages, embeddings):
        ts = msg["timestamp"]
        # Ensure valid ISO 8601 format for DateTimeOffset
        if ts and not ts.endswith("Z") and "+" not in ts:
            ts = ts + "Z"
        search_docs.append(
            {
                "@search.action": "mergeOrUpload",
                "id": msg["message_id"],
                "session_id": msg["session_id"],
                "user_id": msg["user_id"],
                "role": msg["role"],
                "title": build_chat_title(
                    msg["session_name"],
                    msg["role"],
                    msg["intent"],
                    msg["content"],
                ),
                "content": msg["content"],
                "content_vector": vector,
                "intent": msg["intent"],
                "timestamp": ts if ts else None,
                "session_name": msg["session_name"],
            }
        )

    # Upload in batches of 100
    indexed = 0
    for i in range(0, len(search_docs), 100):
        batch = search_docs[i : i + 100]
        if await clients.upload_docs("chat-history-index", batch):
            indexed += len(batch)
            logger.info("    Indexed batch %d–%d", i + 1, i + len(batch))
        else:
            logger.error("    Failed batch %d–%d", i + 1, i + len(batch))
        await asyncio.sleep(0.3)

    await cosmos.close()
    logger.info("  ✅ Backfilled %d/%d messages", indexed, len(search_docs))
    return indexed


# ═══════════════════════════════════════════════════════════════════
# Step 5: DataSource + Skillset + Indexer for chat-history-index
# ─── Self-healing layer: el push fire-and-forget desde el backend
# ─── es la ruta rápida (latencia ~0). El indexer corre cada 5 min
# ─── leyendo el Change Feed (HighWaterMark sobre _ts) y reindexa
# ─── lo que el push haya perdido.
# ═══════════════════════════════════════════════════════════════════


def build_chat_datasource() -> dict:
    """Cosmos DataSource con HighWaterMark sobre c._ts del padre.

    El JOIN m IN c.messages aplana el array; cada modificación de la sesión
    (mensaje añadido) reescribe todos los msgs hijos — aceptable porque las
    sesiones tienen pocas docenas de mensajes.
    """
    query = (
        "SELECT VALUE { "
        "id: m.id ?? CONCAT(c.id, '-', m.timestamp ?? ''), "
        "session_id: c.id, "
        "user_id: c.user_id, "
        "session_name: c.session_name, "
        "role: m.role, "
        "title: CONCAT(c.session_name ?? 'Chat session', ' | ', m.role ?? 'message'), "
        "content: m.content, "
        "timestamp: m.timestamp, "
        "intent: (IS_DEFINED(m.metadata) ? m.metadata.intent : '') ?? '', "
        "_ts: c._ts "
        "} FROM c JOIN m IN c.messages "
        "WHERE c._ts >= @HighWaterMark AND IS_DEFINED(m.content) "
        "AND LENGTH(m.content) > 2"
    )

    return {
        "name": CHAT_DATASOURCE_NAME,
        "type": "cosmosdb",
        "credentials": {"connectionString": COSMOS_CONNECTION_STRING},
        "container": {"name": COSMOS_CHAT_CONTAINER, "query": query},
        "dataChangeDetectionPolicy": {
            "@odata.type": "#Microsoft.Azure.Search.HighWaterMarkChangeDetectionPolicy",
            "highWaterMarkColumnName": "_ts",
        },
    }


def build_chat_skillset() -> dict:
    """Skillset con AzureOpenAIEmbeddingSkill — genera content_vector al vuelo.

    Esto garantiza que docs entrados vía indexer tengan embedding (sin esto
    el indexer pushearía content sin vector y la búsqueda híbrida fallaría).
    """
    return {
        "name": CHAT_SKILLSET_NAME,
        "description": "Genera embeddings para chat-history-index",
        "skills": [
            {
                "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
                "name": "embed-content",
                "context": "/document",
                "resourceUri": OPENAI_ENDPOINT,
                "deploymentId": EMBEDDING_DEPLOYMENT,
                "modelName": EMBEDDING_DEPLOYMENT,
                "dimensions": EMBEDDING_DIMS,
                "inputs": [{"name": "text", "source": "/document/content"}],
                "outputs": [{"name": "embedding", "targetName": "content_vector"}],
            }
        ],
    }


def build_chat_indexer() -> dict:
    return {
        "name": CHAT_INDEXER_NAME,
        "dataSourceName": CHAT_DATASOURCE_NAME,
        "targetIndexName": "chat-history-index",
        "skillsetName": CHAT_SKILLSET_NAME,
        "schedule": {"interval": CHAT_INDEXER_SCHEDULE_INTERVAL},
        "parameters": {
            "batchSize": 100,
            "maxFailedItems": 10,
            "maxFailedItemsPerBatch": 5,
        },
        "fieldMappings": [
            {"sourceFieldName": "id", "targetFieldName": "id"},
            {"sourceFieldName": "session_id", "targetFieldName": "session_id"},
            {"sourceFieldName": "user_id", "targetFieldName": "user_id"},
            {"sourceFieldName": "session_name", "targetFieldName": "session_name"},
            {"sourceFieldName": "role", "targetFieldName": "role"},
            {"sourceFieldName": "title", "targetFieldName": "title"},
            {"sourceFieldName": "content", "targetFieldName": "content"},
            {"sourceFieldName": "timestamp", "targetFieldName": "timestamp"},
            {"sourceFieldName": "intent", "targetFieldName": "intent"},
        ],
        "outputFieldMappings": [
            {
                "sourceFieldName": "/document/content_vector",
                "targetFieldName": "content_vector",
            }
        ],
    }


async def setup_chat_indexer(clients: AzureClients) -> bool:
    """Crea/actualiza DataSource + Skillset + Indexer para chat-history-index."""
    if not COSMOS_CONNECTION_STRING:
        logger.warning(
            "  ⚠️  COSMOSDB_CONNECTION_STRING no está en .env — saltando indexer continuo."
        )
        logger.warning(
            "      Sin esto el chat-history-index depende solo del push fire-and-forget."
        )
        return False

    logger.info("  Creando DataSource: %s", CHAT_DATASOURCE_NAME)
    if not await clients.put_resource(
        "datasources", CHAT_DATASOURCE_NAME, build_chat_datasource()
    ):
        return False

    logger.info("  Creando Skillset: %s", CHAT_SKILLSET_NAME)
    if not await clients.put_resource(
        "skillsets", CHAT_SKILLSET_NAME, build_chat_skillset()
    ):
        return False

    logger.info(
        "  Creando Indexer: %s (schedule=%s)",
        CHAT_INDEXER_NAME,
        CHAT_INDEXER_SCHEDULE_INTERVAL,
    )
    if not await clients.put_resource(
        "indexers", CHAT_INDEXER_NAME, build_chat_indexer()
    ):
        return False

    logger.info("  Disparando primera ejecución del indexer...")
    if await clients.run_indexer(CHAT_INDEXER_NAME):
        logger.info("  ✅ Indexer disparado — primera corrida en marcha")
    else:
        logger.warning(
            "  ⚠️  No se pudo disparar el indexer manualmente (correrá según schedule)"
        )

    return True


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════


async def main():
    logger.info("=" * 60)
    logger.info("MACAE AI Search Pipeline Setup")
    logger.info("=" * 60)
    logger.info("Search:    %s", SEARCH_ENDPOINT)
    logger.info("OpenAI:    %s", OPENAI_ENDPOINT)
    logger.info("Embedding: %s (%d dims)", EMBEDDING_DEPLOYMENT, EMBEDDING_DIMS)
    logger.info("Cosmos:    %s", COSMOS_ENDPOINT[:60] if COSMOS_ENDPOINT else "NOT SET")
    logger.info("")

    if not SEARCH_ENDPOINT:
        logger.error("AZURE_AI_SEARCH_ENDPOINT not set. Aborting.")
        sys.exit(1)
    if not OPENAI_ENDPOINT:
        logger.error("AZURE_OPENAI_ENDPOINT not set. Aborting.")
        sys.exit(1)

    clients = AzureClients()
    await clients.init_session()

    try:
        # Step 0
        logger.info("── Step 0: Migrate doc indices to canonical schema ───")
        migrated = await migrate_doc_indices_to_canonical(clients)
        total_doc_indices = len(EXISTING_INDICES) + len(NEW_DOC_INDICES)
        logger.info("Result: %d/%d doc indices canonical\n", migrated, total_doc_indices)

        # Step 1
        logger.info("── Step 1: Upgrade existing indices ──────────────────")
        upgraded = await upgrade_existing_indices(clients)
        logger.info("Result: %d/%d indices upgraded\n", upgraded, len(EXISTING_INDICES))

        # Step 2
        logger.info("── Step 2: Create chat-history-index ─────────────────")
        created = await create_chat_history_index(clients)
        logger.info("")

        # Step 3
        logger.info("── Step 3: Backfill embeddings for existing docs ─────")
        docs_count = await backfill_existing_docs(clients)
        logger.info("Result: %d documents embedded\n", docs_count)

        # Step 4
        logger.info("── Step 4: Backfill chat history from Cosmos DB ──────")
        msgs_count = await backfill_chat_history(clients)
        logger.info("Result: %d messages indexed\n", msgs_count)

        # Step 5
        logger.info("── Step 5: DataSource + Indexer (Cosmos→chat-history) ─")
        indexer_ok = await setup_chat_indexer(clients)
        logger.info("")

        # Summary
        logger.info("=" * 60)
        logger.info("SUMMARY")
        logger.info("=" * 60)
        logger.info("  Doc indices canonical: %d/%d", migrated, total_doc_indices)
        logger.info("  Indices upgraded:      %d/%d", upgraded, len(EXISTING_INDICES))
        logger.info("  Chat index created:    %s", "✅" if created else "❌")
        logger.info("  Doc embeddings:        %d", docs_count)
        logger.info("  Chat msgs indexed:     %d", msgs_count)
        logger.info(
            "  Total indices:         %d",
            total_doc_indices + (1 if created else 0),
        )
        logger.info(
            "  Continuous indexer:   %s",
            "✅ configured"
            if indexer_ok
            else "⚠️  skipped (no COSMOSDB_CONNECTION_STRING)",
        )
        logger.info("=" * 60)
        logger.info("")
        logger.info("Next steps:")
        logger.info(
            "  1. Add AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small to .env"
        )
        logger.info(
            "  2. Restart backend — new messages auto-indexed via SearchIndexService"
        )
        logger.info(
            "  3. Both MCP and conversational agents now use hybrid search for context"
        )
        if not indexer_ok:
            logger.warning(
                "  ⚠️  Set COSMOSDB_CONNECTION_STRING in .env and re-run to enable "
                "the continuous indexer (Step 5)"
            )

    finally:
        await clients.close()


if __name__ == "__main__":
    asyncio.run(main())
