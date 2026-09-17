"""Focused shared-object-storage contracts for the uploads router."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from _router_auth_helpers import call_unwrapped
from fastapi import HTTPException, UploadFile

from app.gateway.routers import uploads as router
from deerflow.object_storage import ObjectKeyNamespace, ObjectMetadata, ObjectRead, ObjectStorage, UploadsStorage


class _MemoryObjects(ObjectStorage):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def _metadata(self, key: str) -> ObjectMetadata:
        payload = self.objects[key]
        return ObjectMetadata(key=key, size=len(payload), etag=None, content_type=None, last_modified=None, checksum_sha256=None, user_metadata={})

    async def write_stream(self, key: str, chunks: AsyncIterable[bytes], *, content_type: str | None = None, metadata=None) -> ObjectMetadata:
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
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield self._metadata(key)

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def copy(self, source_key: str, destination_key: str) -> ObjectMetadata:
        self.objects[destination_key] = self.objects[source_key]
        return self._metadata(destination_key)


class ChunkedUpload:
    def __init__(self, filename: str, chunks: list[bytes]) -> None:
        self.filename = filename
        self._chunks = chunks
        self.read_calls: list[int] = []

    async def read(self, size: int) -> bytes:
        self.read_calls.append(size)
        return self._chunks.pop(0) if self._chunks else b""


@pytest.fixture
def storage(monkeypatch):
    objects = _MemoryObjects()

    class Factory:
        @classmethod
        def from_app_config(cls, _config, *, user_id: str, thread_id: str):
            return UploadsStorage(objects, namespace=ObjectKeyNamespace(), user_id=user_id, thread_id=thread_id)

    monkeypatch.setattr(router, "UploadsStorage", Factory)
    monkeypatch.setattr(router, "get_effective_user_id", lambda: "user-1")
    return UploadsStorage(objects, namespace=ObjectKeyNamespace(), user_id="user-1", thread_id="thread-1"), objects


@pytest.mark.asyncio
async def test_upload_streams_body_to_shared_storage_and_preserves_duplicate_names(storage):
    _, objects = storage
    first = ChunkedUpload("data.txt", [b"one", b""])
    second = ChunkedUpload("data.txt", [b"two", b""])
    result = await call_unwrapped(
        router.upload_files,
        "thread-1",
        request=SimpleNamespace(),
        config=SimpleNamespace(),
        files=[first, second],
    )

    assert [item.filename for item in result.files] == ["data.txt", "data_1.txt"]
    assert first.read_calls == [router.UPLOAD_CHUNK_SIZE, router.UPLOAD_CHUNK_SIZE]
    assert objects.objects[next(key for key in objects.objects if key.endswith("/data.txt"))] == b"one"
    assert objects.objects[next(key for key in objects.objects if key.endswith("/data_1.txt"))] == b"two"


@pytest.mark.asyncio
async def test_upload_rejects_oversized_stream_without_a_persistent_local_file(storage):
    _, objects = storage
    config = SimpleNamespace(uploads={"max_file_size": 3})
    with pytest.raises(HTTPException, match="File too large"):
        await call_unwrapped(
            router.upload_files,
            "thread-1",
            request=SimpleNamespace(),
            config=config,
            files=[ChunkedUpload("large.txt", [b"four", b""])],
        )
    assert objects.objects == {}


@pytest.mark.asyncio
async def test_list_and_delete_use_shared_upload_objects(storage):
    uploads, _ = storage

    async def payload(value: bytes):
        yield value

    await uploads.write_stream("report.pdf", payload(b"pdf"))
    await uploads.write_stream("report.md", payload(b"markdown"))
    config = SimpleNamespace()
    listed = await call_unwrapped(router.list_uploaded_files, "thread-1", request=SimpleNamespace(), config=config)
    assert [item.filename for item in listed.files] == ["report.md", "report.pdf"]

    deleted = await call_unwrapped(router.delete_uploaded_file, "thread-1", "report.pdf", request=SimpleNamespace(), config=config)
    assert deleted["success"] is True
    assert await uploads.list() == []


@pytest.mark.asyncio
async def test_document_conversion_uses_a_temporary_materialization(storage, monkeypatch):
    uploads, _ = storage

    async def convert(source, *, output_path):
        assert source.parent.name.startswith("deerflow-upload-")
        assert output_path.parent == source.parent
        output_path.write_text("# Converted", encoding="utf-8")
        return output_path

    convert_mock = AsyncMock(side_effect=convert)
    monkeypatch.setattr(router, "convert_file_to_markdown", convert_mock)
    result = await call_unwrapped(
        router.upload_files,
        "thread-1",
        request=SimpleNamespace(),
        config=SimpleNamespace(uploads={"auto_convert_documents": True}),
        files=[UploadFile(filename="report.pdf", file=BytesIO(b"pdf"))],
    )

    assert result.files[0].markdown_file == "report.md"
    assert [item.filename for item in await uploads.list()] == ["report.md", "report.pdf"]


@pytest.mark.asyncio
async def test_object_storage_configuration_failure_is_not_a_local_fallback(monkeypatch, tmp_path):
    class Unavailable:
        @classmethod
        def from_app_config(cls, *_args, **_kwargs):
            raise RuntimeError("S3 unavailable")

    monkeypatch.setattr(router, "UploadsStorage", Unavailable)
    with pytest.raises(HTTPException) as error:
        await call_unwrapped(
            router.upload_files,
            "thread-1",
            request=SimpleNamespace(),
            config=SimpleNamespace(),
            files=[UploadFile(filename="report.txt", file=BytesIO(b"payload"))],
        )
    assert error.value.status_code == 503
    assert list(tmp_path.iterdir()) == []
