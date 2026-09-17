"""Focused contract tests for the Phase 5 shared object-storage foundation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from deerflow.config.app_config import AppConfig
from deerflow.config.object_storage_config import ObjectStorageConfig
from deerflow.object_storage import ByteRange, ObjectKeyNamespace, ObjectStorageConfigurationError, S3ObjectStorage


class _Body:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = 0

    async def iter_chunks(self, *, chunk_size: int):  # noqa: ARG002 - matches aiobotocore streaming body
        for chunk in self.chunks:
            yield chunk

    async def close(self) -> None:
        self.closed += 1


class _Paginator:
    async def paginate(self, **kwargs):  # noqa: ARG002 - S3 paginator shape
        yield {"Contents": [{"Key": "deer-flow/v1/a", "Size": 4, "ETag": '"etag-a"'}]}


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.body = _Body([b"bc", b"d"])

    async def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        return {"ETag": '"etag-put"', "ChecksumSHA256": "checksum"}

    async def create_multipart_upload(self, **kwargs):
        self.calls.append(("create_multipart_upload", kwargs))
        return {"UploadId": "upload-1"}

    async def upload_part(self, **kwargs):
        self.calls.append(("upload_part", kwargs))
        return {"ETag": f'"part-{kwargs["PartNumber"]}"'}

    async def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete_multipart_upload", kwargs))
        return {"ETag": '"etag-complete"'}

    async def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort_multipart_upload", kwargs))

    async def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        return {"Body": self.body, "ContentLength": 3, "ETag": '"etag-read"', "ContentType": "text/plain"}

    async def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        return {"ContentLength": 4, "ETag": '"etag-head"', "LastModified": datetime(2026, 9, 17, tzinfo=UTC)}

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        return _Paginator()

    async def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))

    async def copy_object(self, **kwargs):
        self.calls.append(("copy_object", kwargs))


def _storage(client: _Client) -> S3ObjectStorage:
    @asynccontextmanager
    async def client_context():
        yield client

    return S3ObjectStorage(ObjectStorageConfig(enabled=True, bucket="deer-flow-test"), client_factory=client_context)


def test_object_key_namespace_is_stable_and_confined():
    keys = ObjectKeyNamespace()

    assert keys.artifact("user_1", "thread-1", "reports/final.csv") == "deer-flow/v1/users/user_1/threads/thread-1/outputs/reports/final.csv"
    assert keys.tool_result("user_1", "thread-1", "call.json") == "deer-flow/v1/users/user_1/threads/thread-1/outputs/.tool-results/call.json"
    assert keys.upload("user_1", "thread-1", "source.pdf") == "deer-flow/v1/users/user_1/threads/thread-1/uploads/source.pdf"
    assert keys.custom_skill("user_1", "release-notes", "SKILL.md") == "deer-flow/v1/users/user_1/skills/custom/release-notes/SKILL.md"
    assert keys.skill_state("user_1") == "deer-flow/v1/users/user_1/skills/_skill_states.json"

    with pytest.raises(ValueError, match="stay under"):
        keys.artifact("user_1", "thread-1", "../outside")


def test_object_storage_config_requires_a_bucket_only_when_enabled():
    assert ObjectStorageConfig().enabled is False
    assert ObjectStorageConfig(enabled=True, bucket="deer-flow-test").force_path_style is True
    with pytest.raises(ValueError, match="bucket"):
        ObjectStorageConfig(enabled=True)

    app_config = AppConfig(sandbox={"use": "test"}, object_storage={"enabled": True, "bucket": "deer-flow-test"})
    assert app_config.object_storage.bucket == "deer-flow-test"


@pytest.mark.asyncio
async def test_s3_port_streams_writes_and_range_reads_without_local_fallback():
    client = _Client()
    storage = _storage(client)

    async def chunks():
        yield b"a"
        yield b"b"

    written = await storage.write_stream("deer-flow/v1/key", chunks(), content_type="text/plain", metadata={"origin": "test"})
    assert written.etag == '"etag-put"'
    put = next(call for call in client.calls if call[0] == "put_object")[1]
    assert put["Body"] == b"ab"
    assert put["ChecksumAlgorithm"] == "SHA256"

    async with storage.open_read("deer-flow/v1/key", byte_range=ByteRange(1, 3)) as read:
        assert b"".join([chunk async for chunk in read.chunks]) == b"bcd"
        assert read.metadata.etag == '"etag-read"'
    assert next(call for call in client.calls if call[0] == "get_object")[1]["Range"] == "bytes=1-3"
    assert client.body.closed == 1

    with pytest.raises(ObjectStorageConfigurationError):
        S3ObjectStorage(ObjectStorageConfig())


@pytest.mark.asyncio
async def test_s3_port_uses_multipart_for_large_streams_and_propagates_outages():
    client = _Client()
    storage = _storage(client)

    async def large_chunks():
        yield b"a" * (5 * 1024 * 1024)
        yield b"b"

    written = await storage.write_stream("deer-flow/v1/large", large_chunks())
    assert written.size == (5 * 1024 * 1024) + 1
    assert [name for name, _ in client.calls] == ["create_multipart_upload", "upload_part", "upload_part", "complete_multipart_upload"]
    assert client.calls[1][1]["Body"] == b"a" * (5 * 1024 * 1024)

    class UnavailableClient(_Client):
        async def get_object(self, **kwargs):  # noqa: ARG002 - forced outage
            raise ConnectionError("MinIO unavailable")

    with pytest.raises(ConnectionError, match="unavailable"):
        async with _storage(UnavailableClient()).open_read("deer-flow/v1/key"):
            pass


@pytest.mark.asyncio
async def test_s3_port_stats_lists_copies_and_deletes():
    client = _Client()
    storage = _storage(client)

    stat = await storage.stat("deer-flow/v1/a")
    listed = [item async for item in storage.list_prefix("deer-flow/v1/")]
    copied = await storage.copy("deer-flow/v1/a", "deer-flow/v1/b")
    await storage.delete("deer-flow/v1/b")

    assert stat.size == 4
    assert [item.key for item in listed] == ["deer-flow/v1/a"]
    assert copied.key == "deer-flow/v1/b"
    assert [name for name, _ in client.calls] == ["head_object", "copy_object", "head_object", "delete_object"]
