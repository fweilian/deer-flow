"""Focused shared-storage behavior for ``UploadsMiddleware``."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage

from deerflow.agents.middlewares import uploads_middleware as module
from deerflow.object_storage import ObjectKeyNamespace, ObjectMetadata, ObjectRead, ObjectStorage, UploadsStorage


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
        payload = self.objects[key]

        async def chunks() -> AsyncIterator[bytes]:
            yield payload

        yield ObjectRead(self._metadata(key), chunks())

    async def stat(self, key: str) -> ObjectMetadata:
        if key not in self.objects:
            raise FileNotFoundError(key)
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


@pytest.fixture
def shared_uploads(monkeypatch):
    objects = _MemoryObjects()

    class Factory:
        @classmethod
        def from_app_config(cls, _config, *, user_id: str, thread_id: str):
            return UploadsStorage(objects, namespace=ObjectKeyNamespace(), user_id=user_id, thread_id=thread_id)

    monkeypatch.setattr(module, "UploadsStorage", Factory)
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: SimpleNamespace())
    return UploadsStorage(objects, namespace=ObjectKeyNamespace(), user_id="user-1", thread_id="thread-1")


def _runtime():
    runtime = MagicMock()
    runtime.context = {"thread_id": "thread-1", "user_id": "user-1"}
    return runtime


@pytest.mark.asyncio
async def test_middleware_verifies_current_upload_and_materializes_outline_from_shared_storage(shared_uploads):
    async def content():
        yield b"# Shared heading\n\nBody"

    await shared_uploads.write_stream("report.md", content())
    middleware = module.UploadsMiddleware()
    state = {"messages": [HumanMessage(content="analyse", additional_kwargs={"files": [{"filename": "report.md", "size": 1}]})]}

    result = await middleware.abefore_agent(state, _runtime())

    assert result is not None
    assert result["uploaded_files"] == [{"filename": "report.md", "size": 22, "path": "/mnt/user-data/uploads/report.md", "extension": ".md"}]
    assert "Shared heading" in result["messages"][-1].content


@pytest.mark.asyncio
async def test_middleware_does_not_treat_a_local_projection_as_an_upload(shared_uploads):
    middleware = module.UploadsMiddleware()
    state = {"messages": [HumanMessage(content="analyse", additional_kwargs={"files": [{"filename": "missing.txt", "size": 8}]})]}

    result = await middleware.abefore_agent(state, _runtime())

    assert result == {"uploaded_files": []}
