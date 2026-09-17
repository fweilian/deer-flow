"""Focused shared-storage contracts for the historical uploads tool."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deerflow.object_storage import ObjectKeyNamespace, ObjectMetadata, ObjectRead, ObjectStorage, UploadsStorage
from deerflow.tools.builtins import list_uploaded_files_tool as module


class _MemoryObjects(ObjectStorage):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def _metadata(self, key: str) -> ObjectMetadata:
        payload = self.objects[key]
        return ObjectMetadata(key=key, size=len(payload), etag=None, content_type=None, last_modified=None, checksum_sha256=None, user_metadata={})

    async def write_stream(self, key: str, chunks: AsyncIterable[bytes], *, content_type=None, metadata=None) -> ObjectMetadata:
        self.objects[key] = b"".join([chunk async for chunk in chunks])
        return replace(self._metadata(key), content_type=content_type)

    @asynccontextmanager
    async def open_read(self, key: str, *, byte_range=None) -> AsyncIterator[ObjectRead]:
        async def chunks() -> AsyncIterator[bytes]:
            yield self.objects[key]

        yield ObjectRead(self._metadata(key), chunks())

    async def stat(self, key: str) -> ObjectMetadata:
        return self._metadata(key)

    async def list_prefix(self, prefix: str) -> AsyncIterator[ObjectMetadata]:
        for key in self.objects:
            if key.startswith(prefix):
                yield self._metadata(key)

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def copy(self, source_key: str, destination_key: str) -> ObjectMetadata:
        self.objects[destination_key] = self.objects[source_key]
        return self._metadata(destination_key)


@pytest.mark.asyncio
async def test_historical_list_reads_shared_storage_and_excludes_current_run(monkeypatch):
    objects = _MemoryObjects()
    storage = UploadsStorage(objects, namespace=ObjectKeyNamespace(), user_id="user-1", thread_id="thread-1")

    async def content(value: bytes):
        yield value

    await storage.write_stream("old.txt", content(b"old"))
    await storage.write_stream("new.txt", content(b"new"))

    class Factory:
        @classmethod
        def from_app_config(cls, _config, *, user_id: str, thread_id: str):
            return UploadsStorage(objects, namespace=ObjectKeyNamespace(), user_id=user_id, thread_id=thread_id)

    monkeypatch.setattr(module, "UploadsStorage", Factory)
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: SimpleNamespace())
    runtime = MagicMock()
    runtime.context = {"thread_id": "thread-1", "user_id": "user-1"}
    runtime.state = {"uploaded_files": [{"filename": "new.txt"}]}

    result = await module._list_uploaded_files_shared_impl(runtime=runtime)

    assert result["total_count"] == 1
    assert result["files"] == [{"filename": "old.txt", "size": 3, "path": "/mnt/user-data/uploads/old.txt", "extension": ".txt"}]
