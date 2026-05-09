"""AI Search integration: chat history indexing + semantic/hybrid retrieval.

Pattern: Cosmos DB (store) + AI Search (retrieval layer) with embeddings.
- Pushes chat messages to AI Search with vector embeddings on add_message()
- Queries with hybrid search (keyword + vector + semantic reranking) for context

Replaces the sliding-window-of-10 pattern with semantic retrieval across
the ENTIRE conversation history — no message limit.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from common.config.app_config import config

logger = logging.getLogger(__name__)

CHAT_HISTORY_INDEX = "chat-history-index"
SEARCH_API_VERSION = "2024-07-01"
OPENAI_API_VERSION = "2024-10-21"

# All knowledge indices (documents + chat history)
DOCUMENT_INDICES = [
    "contract-compliance-doc-index",
    "contract-risk-doc-index",
    "contract-summary-doc-index",
    "macae-retail-customer-index",
    "macae-retail-order-index",
    "macae-rfp-compliance-index",
    "macae-rfp-risk-index",
    "macae-rfp-summary-index",
]
ALL_INDICES = [CHAT_HISTORY_INDEX] + DOCUMENT_INDICES


class SearchIndexService:
    """AI Search service for embedding, indexing, and hybrid search over chat history."""

    def __init__(self) -> None:
        # credential can be different async credential implementations
        # (DefaultAzureCredential, ManagedIdentityCredential, etc.),
        # so annotate as Any to avoid overly strict type checks.
        self._credential: Optional[Any] = None
        self._search_endpoint: str = ""
        self._openai_endpoint: str = ""
        self._embedding_deployment: str = ""
        self._initialized: bool = False

    async def initialize(self) -> None:
        """Lazy init: set up endpoints and credential."""
        if self._initialized:
            return

        self._search_endpoint = (config.AZURE_AI_SEARCH_ENDPOINT or "").rstrip("/")
        self._openai_endpoint = (config.AZURE_OPENAI_ENDPOINT or "").rstrip("/")
        self._embedding_deployment = getattr(
            config, "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
        )

        if not self._search_endpoint or not self._openai_endpoint:
            logger.warning(
                "SearchIndexService: AZURE_AI_SEARCH_ENDPOINT or AZURE_OPENAI_ENDPOINT "
                "not set — search indexing DISABLED."
            )
            return

        self._credential = config.get_azure_credential_async()
        if not self._credential:
            logger.warning(
                "SearchIndexService: Azure credential not available — search indexing DISABLED."
            )
            return

        self._initialized = True
        logger.info(
            "SearchIndexService initialized (search=%s, embedding=%s)",
            self._search_endpoint,
            self._embedding_deployment,
        )

    async def _ensure(self) -> bool:
        """Ensure initialized. Returns False if disabled."""
        if not self._initialized:
            await self.initialize()
        return self._initialized

    # ── Token helpers ────────────────────────────────────────────

    async def _get_search_token(self) -> str:
        if self._credential is None:
            raise RuntimeError(
                "SearchIndexService: credential is not available. Ensure initialize() succeeded."
            )
        token = await self._credential.get_token("https://search.azure.com/.default")
        return token.token

    async def _get_openai_token(self) -> str:
        if self._credential is None:
            raise RuntimeError(
                "SearchIndexService: credential is not available. Ensure initialize() succeeded."
            )
        token = await self._credential.get_token(
            "https://cognitiveservices.azure.com/.default"
        )
        return token.token

    # ── Embedding generation ─────────────────────────────────────

    async def generate_embedding(self, text: str) -> Optional[List[float]]:
        """Generate embedding vector via Azure OpenAI."""
        if not await self._ensure():
            return None

        # The embeddings API rejects empty / whitespace-only inputs with
        # 400 "$.input is invalid" — skip indexing for those.
        if not text or not text.strip():
            return None

        # Sanitize: remove control characters (except newline/tab) that can
        # cause "$.input is invalid" errors from the OpenAI embeddings API.
        import re

        sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        sanitized = sanitized.strip()
        if not sanitized:
            logger.debug("Skipping embedding: text empty after sanitization")
            return None

        url = (
            f"{self._openai_endpoint}/openai/deployments/{self._embedding_deployment}"
            f"/embeddings?api-version={OPENAI_API_VERSION}"
        )
        token = await self._get_openai_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        # Azure OpenAI embeddings API expects "input" as a string, not array
        truncated_text = sanitized[:8000]
        body = {"input": truncated_text}
        logger.debug("Embedding request: input length=%d chars", len(truncated_text))

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=body) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["data"][0]["embedding"]
                    else:
                        body_text = await resp.text()
                        logger.error(
                            "Embedding API error: HTTP %s — %s",
                            resp.status,
                            body_text[:300],
                        )
                        return None
        except Exception as e:
            logger.error("Embedding generation failed: %s", e)
            return None

    # ── Index a chat message ─────────────────────────────────────

    async def index_chat_message(
        self,
        message_id: str,
        session_id: str,
        user_id: str,
        role: str,
        content: str,
        intent: str = "",
        timestamp: str = "",
        session_name: str = "",
    ) -> bool:
        """Push a single chat message to chat-history-index with its embedding."""
        if not await self._ensure():
            return False

        # Generate embedding for the message content
        vector = await self.generate_embedding(content)
        if vector is None:
            logger.warning(
                "Skipping indexing for message %s — embedding failed", message_id
            )
            return False

        # Ensure timestamp is valid ISO 8601 for DateTimeOffset
        ts = timestamp
        if ts and not ts.endswith("Z") and "+" not in ts:
            ts = ts + "Z"

        doc = {
            "@search.action": "mergeOrUpload",
            "id": message_id,
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            "content": content,
            "content_vector": vector,
            "intent": intent or "",
            "timestamp": ts if ts else None,
            "session_name": session_name or "",
        }

        url = (
            f"{self._search_endpoint}/indexes/{CHAT_HISTORY_INDEX}"
            f"/docs/index?api-version={SEARCH_API_VERSION}"
        )
        token = await self._get_search_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers, json={"value": [doc]}
                ) as resp:
                    if resp.status in (200, 201):
                        return True
                    else:
                        body = await resp.text()
                        logger.error(
                            "Index push failed: HTTP %s — %s",
                            resp.status,
                            body[:300],
                        )
                        return False
        except Exception as e:
            logger.error("Index push error: %s", e)
            return False

    # ── Hybrid search for context retrieval ──────────────────────

    async def search_chat_history(
        self,
        query: str,
        user_id: str = "",
        session_id: str = "",
        top_k: int = 15,
    ) -> List[Dict[str, Any]]:
        """Hybrid search (keyword + vector + semantic reranking) over chat history.

        Returns the top_k most relevant messages across ALL sessions,
        sorted by semantic relevance — not limited by any sliding window.
        """
        if not await self._ensure():
            return []

        # Generate query embedding for vector search
        query_vector = await self.generate_embedding(query)
        if query_vector is None:
            return []

        # Build hybrid search request (keyword + vector + semantic)
        search_body: Dict[str, Any] = {
            "search": query,
            "queryType": "semantic",
            "semanticConfiguration": "chat-semantic-config",
            "top": top_k,
            "select": "id,session_id,user_id,role,content,intent,timestamp,session_name",
            "vectorQueries": [
                {
                    "kind": "vector",
                    "vector": query_vector,
                    "fields": "content_vector",
                    "k": top_k,
                    "exhaustive": False,
                }
            ],
        }

        # Optional filters
        filters = []
        if user_id:
            filters.append(f"user_id eq '{user_id}'")
        if session_id:
            filters.append(f"session_id eq '{session_id}'")
        if filters:
            search_body["filter"] = " and ".join(filters)

        url = (
            f"{self._search_endpoint}/indexes/{CHAT_HISTORY_INDEX}"
            f"/docs/search?api-version={SEARCH_API_VERSION}"
        )
        token = await self._get_search_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=search_body) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = []
                        for doc in data.get("value", []):
                            results.append(
                                {
                                    "id": doc.get("id", ""),
                                    "session_id": doc.get("session_id", ""),
                                    "user_id": doc.get("user_id", ""),
                                    "role": doc.get("role", "user"),
                                    "content": doc.get("content", ""),
                                    "intent": doc.get("intent", ""),
                                    "timestamp": doc.get("timestamp", ""),
                                    "session_name": doc.get("session_name", ""),
                                    "score": doc.get("@search.score", 0),
                                    "reranker_score": doc.get(
                                        "@search.rerankerScore", 0
                                    ),
                                }
                            )
                        return results
                    else:
                        body = await resp.text()
                        logger.error(
                            "Chat history search failed: HTTP %s — %s",
                            resp.status,
                            body[:300],
                        )
                        return []
        except Exception as e:
            logger.error("Chat history search error: %s", e)
            return []

    # ── Multi-index search (memory + knowledge) ─────────────────

    async def search_all_indices(
        self,
        query: str,
        user_id: str = "",
        top_k: int = 15,
        recency_boost: float = 1.3,
        chat_query: str = "",
    ) -> List[Dict[str, Any]]:
        """Search across ALL indices (chat history + documents) in parallel.

        Fuses results by combining semantic score with recency boost for
        chat messages. A recent chat message about "deuda" ranks higher
        than a 3-year-old contract document, unless the document's
        semantic relevance is overwhelming.

        Args:
            query: User's question (natural language).
            user_id: Filter chat history by user.
            top_k: Max results per index.
            recency_boost: Multiplier for chat history scores (>1 = prefer recent).

        Returns:
            Fused list sorted by combined score, max 2*top_k items.
        """
        if not await self._ensure():
            return []

        query_vector = await self.generate_embedding(query)
        if query_vector is None:
            return []

        async def _search_one_index(
            index_name: str, semantic_config: str, is_chat: bool
        ) -> List[Dict[str, Any]]:
            """Search a single index and tag results with source."""
            text_query = (chat_query or query) if is_chat else query
            search_body: Dict[str, Any] = {
                "search": text_query,
                "queryType": "semantic",
                "semanticConfiguration": semantic_config,
                "top": top_k,
                "select": "*",
                "vectorQueries": [
                    {
                        "kind": "vector",
                        "vector": query_vector,
                        "fields": "content_vector",
                        "k": top_k,
                        "exhaustive": False,
                    }
                ],
            }
            if is_chat and user_id:
                search_body["filter"] = f"user_id eq '{user_id}'"

            url = (
                f"{self._search_endpoint}/indexes/{index_name}"
                f"/docs/search?api-version={SEARCH_API_VERSION}"
            )
            token = await self._get_search_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, headers=headers, json=search_body
                    ) as resp:
                        if resp.status != 200:
                            return []
                        data = await resp.json()
                        results = []
                        for doc in data.get("value", []):
                            reranker = doc.get("@search.rerankerScore")
                            base = doc.get("@search.score", 0)
                            score = reranker if reranker is not None else base
                            # Apply recency boost for chat messages
                            if is_chat:
                                score *= recency_boost
                            results.append(
                                {
                                    "content": doc.get("content", ""),
                                    "source_index": index_name,
                                    "source_type": "chat" if is_chat else "document",
                                    "role": doc.get("role", ""),
                                    "title": doc.get("title", ""),
                                    "session_id": doc.get("session_id", ""),
                                    "timestamp": doc.get("timestamp", ""),
                                    "intent": doc.get("intent", ""),
                                    "score": score,
                                }
                            )
                        return results
            except Exception as e:
                logger.debug("Search %s failed: %s", index_name, e)
                return []

        # Search all indices in parallel
        tasks = [
            _search_one_index(CHAT_HISTORY_INDEX, "chat-semantic-config", True)
        ] + [_search_one_index(idx, "default", False) for idx in DOCUMENT_INDICES]

        all_results_nested = await asyncio.gather(*tasks, return_exceptions=True)

        # Flatten and filter errors
        fused: List[Dict[str, Any]] = []
        for result in all_results_nested:
            if isinstance(result, list):
                fused.extend(result)

        # Sort by score descending, take the best results
        fused.sort(key=lambda x: x.get("score", 0), reverse=True)
        final = fused[:10]

        logger.info(
            "search_all_indices: %d results from %d indices (query='%s')",
            len(final),
            sum(1 for r in all_results_nested if isinstance(r, list) and r),
            query[:60],
        )
        return final

    # ── Query expansion via LLM ──────────────────────────────────

    async def expand_query(self, user_message: str) -> str:
        """Use LLM to expand a vague user query into a search-optimized query.

        Example: 'lo que vimos ayer sobre la deuda' →
        'historial conversación deuda obligaciones contractuales riesgo financiero'
        """
        if not await self._ensure():
            return user_message

        try:
            url = (
                f"{self._openai_endpoint}/openai/deployments/"
                f"{getattr(config, 'AZURE_OPENAI_DEPLOYMENT_NAME', 'gpt-4.1-mini')}"
                f"/chat/completions?api-version={OPENAI_API_VERSION}"
            )
            token = await self._get_openai_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            body = {
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a search query optimizer. Given a user message, "
                            "output ONLY a search query (no explanation) that captures "
                            "the semantic intent. Include synonyms and related terms. "
                            "Keep it under 50 words. Respond in the same language."
                        ),
                    },
                    {"role": "user", "content": user_message},
                ],
                "max_tokens": 80,
                "temperature": 0.0,
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=body) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        expanded = data["choices"][0]["message"]["content"].strip()
                        logger.info(
                            "Query expanded: '%s' → '%s'",
                            user_message[:40],
                            expanded[:60],
                        )
                        return expanded
        except Exception as e:
            logger.debug("Query expansion failed: %s", e)

        return user_message


# ── Singleton ────────────────────────────────────────────────────

_instance: Optional[SearchIndexService] = None


async def get_search_index_service() -> SearchIndexService:
    """Get the singleton SearchIndexService (lazy init)."""
    global _instance
    if _instance is None:
        _instance = SearchIndexService()
        await _instance.initialize()
    return _instance
