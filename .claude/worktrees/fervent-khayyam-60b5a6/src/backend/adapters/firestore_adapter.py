"""Firestore adapter using official Google Cloud SDK."""

import json
from typing import Any, Dict

from adapters.base_adapter import BaseAdapter


class FirestoreAdapter(BaseAdapter):
    """Minimal Firestore adapter using official SDK."""

    def _get_provider_id(self) -> str:
        return "firestore"

    async def _execute_with_credentials(
        self,
        tool_name: str,
        params: Dict[str, Any],
        credentials: Dict[str, str],
    ) -> Any:
        """Execute Firestore operations using official SDK."""
        import asyncio
        from google.cloud import firestore

        service_account_json = credentials.get("service_account_json")
        if not service_account_json:
            raise ValueError("service_account_json is required")

        service_account_info = json.loads(service_account_json)

        dispatch = {
            "get_document": self._sync_get_document,
            "create_document": self._sync_create_document,
            "query_documents": self._sync_query_documents,
            "list_collections": self._sync_list_collections,
            "list_documents": self._sync_list_documents,
            "count_documents": self._sync_count_documents,
            "update_document": self._sync_update_document,
            "delete_document": self._sync_delete_document,
            "read_firestore_doc": self._sync_read_firestore_doc,
            "list_subcollections": self._sync_list_subcollections,
            "list_documents_at_path": self._sync_list_documents_at_path,
        }
        handler = dispatch.get(tool_name)
        if not handler:
            raise ValueError(f"Unknown Firestore operation: {tool_name}")

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: handler(
                firestore.Client.from_service_account_info(service_account_info),
                params,
            ),
        )

    # ------------------------------------------------------------------
    # Sync handlers — run inside thread pool, no event loop interaction
    # ------------------------------------------------------------------

    def _sync_read_firestore_doc(self, db, params: Dict[str, Any]) -> Dict[str, Any]:
        doc_path = (params.get("full_path") or params.get("doc_path", "")).strip("/")
        if not doc_path:
            raise ValueError("full_path or doc_path is required")
        parts = doc_path.split("/")
        if len(parts) % 2 != 0:
            return {"success": False, "error": f"Invalid document path: '{doc_path}'."}
        doc = db.document(doc_path).get()
        if not doc.exists:
            return {"exists": False, "data": None}
        return {"exists": True, "data": doc.to_dict(), "id": doc.id}

    def _sync_get_document(self, db, params: Dict[str, Any]) -> Dict[str, Any]:
        full_path = params.get("full_path")
        if not full_path:
            raise ValueError("full_path is required")
        doc = db.document(full_path).get()
        if not doc.exists:
            return {"exists": False, "data": None}
        return {"exists": True, "data": doc.to_dict(), "id": doc.id}

    def _sync_create_document(self, db, params: Dict[str, Any]) -> Dict[str, str]:
        collection = params.get("collection")
        data = params.get("data")
        document_id = params.get("document_id")
        if not collection or not data:
            raise ValueError("collection and data are required")
        col = db.collection(collection)
        doc_ref = col.document(document_id) if document_id else col.document()
        doc_ref.set(data)
        return {"document_id": doc_ref.id, "collection": collection}

    def _sync_query_documents(self, db, params: Dict[str, Any]) -> list:
        collection = params.get("collection")
        limit = params.get("limit", 10)
        where_clauses = params.get("where", [])
        if not collection:
            raise ValueError("collection is required")
        query = db.collection(collection)
        for clause in where_clauses:
            field = clause.get("field")
            operator = clause.get("operator", "==")
            value = clause.get("value")
            if field and value is not None:
                query = query.where(field, operator, value)
        return [{"id": doc.id, **doc.to_dict()} for doc in query.limit(limit).stream()]

    def _sync_update_document(self, db, params: Dict[str, Any]) -> Dict[str, str]:
        collection = params.get("collection")
        document_id = params.get("document_id")
        data = params.get("data")
        if not collection or not document_id or not data:
            raise ValueError("collection, document_id, and data are required")
        db.collection(collection).document(document_id).update(data)
        return {"document_id": document_id, "collection": collection}

    def _sync_delete_document(self, db, params: Dict[str, Any]) -> Dict[str, Any]:
        collection = params.get("collection")
        document_id = params.get("document_id")
        if not collection or not document_id:
            raise ValueError("collection and document_id are required")
        db.collection(collection).document(document_id).delete()
        return {"document_id": document_id, "collection": collection, "deleted": True}

    def _sync_list_collections(self, db, params: Dict[str, Any]) -> list:
        return [{"id": col.id, "collection_id": col.id} for col in db.collections()]

    def _sync_list_documents(self, db, params: Dict[str, Any]) -> list:
        collection = params.get("collection")
        limit = params.get("limit", 100)
        if not collection:
            raise ValueError("collection is required")
        docs = db.collection(collection).limit(limit).stream()
        return [{"id": doc.id, "path": f"{collection}/{doc.id}"} for doc in docs]

    def _sync_list_subcollections(self, db, params: Dict[str, Any]) -> list:
        doc_path = params.get("doc_path", "").strip("/")
        if not doc_path:
            raise ValueError("doc_path is required")
        parts = doc_path.split("/")
        if len(parts) % 2 != 0:
            first = next(iter(db.collection(doc_path).limit(1).stream()), None)
            if not first:
                return []
            doc_ref = first.reference
        else:
            doc_ref = db.document(doc_path)
        return [
            {"id": col.id, "path": f"{doc_ref.path}/{col.id}"}
            for col in doc_ref.collections()
        ]

    def _sync_list_documents_at_path(self, db, params: Dict[str, Any]) -> list:
        collection_path = params.get("collection_path", "").strip("/")
        limit = params.get("limit", 25)
        if not collection_path:
            raise ValueError("collection_path is required")
        parts = collection_path.split("/")
        if len(parts) % 2 != 1:
            raise ValueError(
                f"collection_path must have odd number of segments: {collection_path}"
            )
        col_ref = db.collection(parts[0])
        for i in range(1, len(parts), 2):
            if i + 1 < len(parts):
                col_ref = col_ref.document(parts[i]).collection(parts[i + 1])
        docs = col_ref.limit(limit).stream()
        return [{"id": doc.id, "path": f"{collection_path}/{doc.id}"} for doc in docs]

    def _sync_count_documents(self, db, params: Dict[str, Any]) -> Dict[str, int]:
        collection = params.get("collection")
        if not collection:
            raise ValueError("collection is required")
        count = sum(1 for _ in db.collection(collection).stream())
        return {"collection": collection, "count": count}
