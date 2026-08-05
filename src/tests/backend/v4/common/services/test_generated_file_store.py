"""Unit tests for GeneratedFileStore (blob persistence of generated files).

The blob SDK is mocked by injecting a fake BlobServiceClient into the
instance (no sys.modules stomping): save/load logic is exercised exactly as
the router uses it — save never raises, load misses return None so the
caller can fall back to the live-Foundry read path.
"""

import os
import sys
from unittest.mock import AsyncMock, Mock

import pytest

# Load the module under test by FILE PATH (house pattern): earlier test
# modules stomp sys.modules['v4.*'] with Mocks, so the package route is
# unreliable. Registered under backend.* for coverage tracking.
import importlib.util  # noqa: E402

_svc_path = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), '..', '..', '..', '..', '..',
        'backend', 'v4', 'common', 'services', 'generated_file_store.py',
    )
)
_spec = importlib.util.spec_from_file_location(
    "backend.v4.common.services.generated_file_store", _svc_path
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["backend.v4.common.services.generated_file_store"] = _mod
_spec.loader.exec_module(_mod)

GeneratedFileStore = _mod.GeneratedFileStore
_CONTAINER = _mod._CONTAINER


def _store_with(svc: Mock) -> GeneratedFileStore:
    """Instance with an injected fake BlobServiceClient (skips __init__)."""
    store = GeneratedFileStore.__new__(GeneratedFileStore)
    store._svc = svc
    return store


def _svc_with_container(cc: Mock) -> Mock:
    svc = Mock()
    svc.get_container_client.return_value = cc
    return svc


@pytest.fixture(autouse=True)
def _reset_singleton():
    GeneratedFileStore._instance = None
    yield
    GeneratedFileStore._instance = None


@pytest.fixture(autouse=True)
def _real_azure_core_exceptions():
    """Other test modules stomp sys.modules['azure.core.exceptions'] with a
    bare Mock for the whole session; the service's lazy typed `except` then
    receives a non-exception and TypeErrors. Restore the REAL module for the
    duration of these tests, then put the stomps back untouched."""
    saved = {
        k: sys.modules.get(k)
        for k in ("azure", "azure.core", "azure.core.exceptions")
    }
    for k in saved:
        sys.modules.pop(k, None)
    import azure.core.exceptions  # noqa: F401  (re-imports the real one)

    yield

    for k, v in saved.items():
        if v is not None:
            sys.modules[k] = v
        else:
            sys.modules.pop(k, None)


class TestGeneratedFileStoreSave:
    @pytest.mark.asyncio
    async def test_save_uploads_with_filename_metadata(self):
        cc = Mock()
        cc.upload_blob = AsyncMock()
        store = _store_with(_svc_with_container(cc))

        ok = await store.save("cfile_1", "plot.png", b"\x89PNG")

        assert ok is True
        store._svc.get_container_client.assert_called_once_with(_CONTAINER)
        cc.upload_blob.assert_awaited_once_with(
            name="cfile_1",
            data=b"\x89PNG",
            overwrite=True,
            metadata={"filename": "plot.png"},
        )

    @pytest.mark.asyncio
    async def test_save_failure_returns_false_never_raises(self):
        cc = Mock()
        cc.upload_blob = AsyncMock(side_effect=RuntimeError("blob down"))
        store = _store_with(_svc_with_container(cc))

        ok = await store.save("cfile_1", "plot.png", b"data")

        assert ok is False


class TestGeneratedFileStoreLoad:
    @pytest.mark.asyncio
    async def test_load_hit_returns_bytes_and_filename(self):
        downloader = Mock()
        downloader.readall = AsyncMock(return_value=b"\x89PNG")
        downloader.properties = Mock(metadata={"filename": "plot.png"})
        bc = Mock()
        bc.download_blob = AsyncMock(return_value=downloader)
        cc = Mock()
        cc.get_blob_client.return_value = bc
        store = _store_with(_svc_with_container(cc))

        result = await store.load("cfile_1")

        assert result == (b"\x89PNG", "plot.png")
        cc.get_blob_client.assert_called_once_with("cfile_1")

    @pytest.mark.asyncio
    async def test_load_miss_returns_none(self):
        # Import inside the test: the autouse fixture has restored the REAL
        # azure.core.exceptions here (module top-level would bind the Mock
        # that earlier test modules stomp into sys.modules).
        from azure.core.exceptions import ResourceNotFoundError

        bc = Mock()
        bc.download_blob = AsyncMock(
            side_effect=ResourceNotFoundError("no such blob")
        )
        cc = Mock()
        cc.get_blob_client.return_value = bc
        store = _store_with(_svc_with_container(cc))

        assert await store.load("cfile_missing") is None

    @pytest.mark.asyncio
    async def test_load_error_returns_none_never_raises(self):
        bc = Mock()
        bc.download_blob = AsyncMock(side_effect=RuntimeError("blob down"))
        cc = Mock()
        cc.get_blob_client.return_value = bc
        store = _store_with(_svc_with_container(cc))

        assert await store.load("cfile_1") is None
