"""Persistent store for generated files (code interpreter / gpt-image).

Foundry code-interpreter containers expire ~20 minutes after going idle and
their files (cfile_*) die with them — any generated image older than that
404s ("Container is expired"). This store copies every generated file into
the solution's Blob account AT GENERATION TIME, and /chat/download-file
serves from here first, so files outlive their container.

Persistence is a functional requirement: AZURE_STORAGE_BLOB_URL is a required
config value (infra injects it in the container app; local dev sets it in
.env) and the ``generated-files`` container exists in the account. The
live-Foundry path in download-file remains only as the migration read path
for files created before this store existed.
"""

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_CONTAINER = "generated-files"


class GeneratedFileStore:
    """Blob-backed store keyed by Foundry file_id; filename kept as metadata."""

    _instance: Optional["GeneratedFileStore"] = None

    def __init__(self, blob_url: str) -> None:
        from azure.storage.blob.aio import BlobServiceClient

        from common.config.app_config import config

        # Borrowed process-shared credential: closed by config at shutdown,
        # never here (same ownership rule as the AI project client).
        self._svc = BlobServiceClient(
            account_url=blob_url,
            credential=config.get_shared_async_credential(),
        )

    @classmethod
    def get_instance(cls) -> "GeneratedFileStore":
        if cls._instance is None:
            from common.config.app_config import config

            cls._instance = cls(config.AZURE_STORAGE_BLOB_URL)
        return cls._instance

    @classmethod
    async def aclose_instance(cls) -> None:
        """Close the blob transport at app shutdown (lifespan)."""
        if cls._instance is not None:
            try:
                await cls._instance._svc.close()
            except Exception as ex:
                logger.warning("GeneratedFileStore close failed: %s", ex)
            cls._instance = None

    async def save(self, file_id: str, filename: str, data: bytes) -> bool:
        """Copy one generated file to Blob.

        Logs at ERROR on failure (persistence is a requirement, not best-
        effort) but does not raise: aborting a streaming answer because one
        artifact copy failed would punish the user twice.
        """
        try:
            cc = self._svc.get_container_client(_CONTAINER)
            await cc.upload_blob(
                name=file_id,
                data=data,
                overwrite=True,
                metadata={"filename": filename},
            )
            logger.info(
                "Persisted generated file %s (%s, %d bytes)",
                file_id,
                filename,
                len(data),
            )
            return True
        except Exception as ex:
            logger.error("Persist of generated file %s FAILED: %s", file_id, ex)
            return False

    async def load(self, file_id: str) -> Optional[Tuple[bytes, str]]:
        """Return (bytes, filename), or None when the blob does not exist
        (file predates the store) so the caller uses the Foundry read path."""
        from azure.core.exceptions import ResourceNotFoundError

        try:
            cc = self._svc.get_container_client(_CONTAINER)
            bc = cc.get_blob_client(file_id)
            downloader = await bc.download_blob()
            data = await downloader.readall()
            meta = getattr(downloader.properties, "metadata", None) or {}
            filename = meta.get("filename") or file_id
            return data, filename
        except ResourceNotFoundError:
            return None
        except Exception as ex:
            logger.error("Load of generated file %s FAILED: %s", file_id, ex)
            return None
