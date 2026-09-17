"""Focused Phase 5 outputs adapter contracts."""

from __future__ import annotations

import zipfile
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from app.gateway.artifact_archive import build_object_artifact_archive
from deerflow.object_storage import ByteRange, ObjectKeyNamespace, ObjectMetadata, ObjectRead, ObjectStorage, OutputsStorage
from deerflow.sandbox.output_projection import commit_remote_outputs, hydrate_remote_outputs


class _MemoryObjects(ObjectStorage):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def _metadata(self, key: str) -> ObjectMetadata:
        payload = self.objects[key]
        return ObjectMetadata(key=key, size=len(payload), etag=f'"{payload.hex()}"', content_type=None, last_modified=None, checksum_sha256=None, user_metadata={})

    async def write_stream(self, key: str, chunks: AsyncIterable[bytes], *, content_type: str | None = None, metadata=None) -> ObjectMetadata:
        self.objects[key] = b"".join([chunk async for chunk in chunks])
        return replace(self._metadata(key), content_type=content_type)

    @asynccontextmanager
    async def open_read(self, key: str, *, byte_range: ByteRange | None = None) -> AsyncIterator[ObjectRead]:
        payload = self.objects[key]
        if byte_range is not None:
            payload = payload[byte_range.start : None if byte_range.end is None else byte_range.end + 1]

        async def chunks() -> AsyncIterator[bytes]:
            yield payload

        yield ObjectRead(self._metadata(key), chunks())

    async def stat(self, key: str) -> ObjectMetadata:
        return self._metadata(key)

    async def list_prefix(self, prefix: str) -> AsyncIterator[ObjectMetadata]:
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield self._metadata(key)

    async def delete(self, key: str) -> None:
        del self.objects[key]

    async def copy(self, source_key: str, destination_key: str) -> ObjectMetadata:
        self.objects[destination_key] = self.objects[source_key]
        return self._metadata(destination_key)


def _outputs() -> tuple[OutputsStorage, _MemoryObjects]:
    objects = _MemoryObjects()
    return OutputsStorage(objects, namespace=ObjectKeyNamespace(), user_id="user-1", thread_id="thread-1"), objects


@pytest.mark.asyncio
async def test_outputs_object_reads_are_streamed_and_range_confined() -> None:
    outputs, objects = _outputs()
    await outputs.write_bytes("reports/final.txt", b"abcdef", content_type="text/plain")

    async with outputs.open_read("reports/final.txt", byte_range=ByteRange(1, 3)) as read:
        assert b"".join([chunk async for chunk in read.chunks]) == b"bcd"
    assert [item.relative_path for item in await outputs.list()] == ["reports/final.txt"]
    assert next(iter(objects.objects)).endswith("/outputs/reports/final.txt")

    with pytest.raises(Exception, match="Only files below"):
        outputs.relative_path("/mnt/user-data/uploads/private.txt")


@pytest.mark.asyncio
async def test_projection_is_hydrated_from_and_committed_to_object_storage(tmp_path) -> None:
    outputs, objects = _outputs()
    await outputs.write_bytes("old.txt", b"old")
    projection = tmp_path / "outputs"
    projection.mkdir()
    (projection / "node-local.txt").write_text("must not survive hydration", encoding="utf-8")

    before = await outputs.hydrate_projection(projection)
    assert (projection / "old.txt").read_bytes() == b"old"
    assert not (projection / "node-local.txt").exists()

    (projection / "old.txt").write_bytes(b"updated")
    (projection / "new.txt").write_bytes(b"new")
    after = await outputs.commit_projection(projection)

    assert objects.objects[outputs._key("old.txt")] == b"updated"
    assert objects.objects[outputs._key("new.txt")] == b"new"
    assert OutputsStorage.changed_paths(before, after) == ["/mnt/user-data/outputs/new.txt", "/mnt/user-data/outputs/old.txt"]


@pytest.mark.asyncio
async def test_remote_sandbox_outputs_are_hydrated_and_committed_through_shared_storage() -> None:
    outputs, objects = _outputs()
    await outputs.write_bytes("old.txt", b"old")

    class RemoteSandbox:
        def __init__(self) -> None:
            self.files = {"/mnt/user-data/outputs/node-local.txt": b"stale"}

        def execute_command(self, command: str) -> str:
            if command.startswith("rm -rf"):
                self.files.clear()
            return ""

        def update_file(self, path: str, content: bytes) -> None:
            self.files[path] = content

        def glob(self, _path: str, _pattern: str, *, include_dirs: bool, max_results: int):  # noqa: ARG002
            return sorted(self.files), False

        def download_file(self, path: str) -> bytes:
            return self.files[path]

    sandbox = RemoteSandbox()
    await hydrate_remote_outputs(outputs, sandbox)
    assert sandbox.files == {"/mnt/user-data/outputs/old.txt": b"old"}

    sandbox.files.pop("/mnt/user-data/outputs/old.txt")
    sandbox.files["/mnt/user-data/outputs/report.txt"] = b"report"
    await commit_remote_outputs(outputs, sandbox)

    assert [item.relative_path for item in await outputs.list()] == ["report.txt"]
    assert objects.objects[outputs._key("report.txt")] == b"report"


@pytest.mark.asyncio
async def test_archive_reads_presented_artifacts_from_shared_outputs() -> None:
    outputs, _ = _outputs()
    await outputs.write_bytes("reports/final.txt", b"final")

    result = await build_object_artifact_archive(outputs, ["/mnt/user-data/outputs/reports/final.txt"])
    with result.file, zipfile.ZipFile(result.file) as archive:
        assert archive.namelist() == ["reports/final.txt"]
        assert archive.read("reports/final.txt") == b"final"
