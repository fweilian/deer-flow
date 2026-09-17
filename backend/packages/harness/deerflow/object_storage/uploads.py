"""Uploads-domain facade backed exclusively by shared object storage.

The sandbox may receive a disposable filesystem projection while it executes,
but uploaded objects are always read from and written to the shared store.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from deerflow.object_storage.keys import ObjectKeyNamespace
from deerflow.object_storage.port import ObjectMetadata, ObjectStorage, get_object_storage

UPLOADS_VIRTUAL_PREFIX = "/mnt/user-data/uploads"
_CHUNK_SIZE = 64 * 1024


class UploadStorageError(RuntimeError):
    """Raised when an upload cannot safely be addressed or projected."""


@dataclass(frozen=True)
class UploadObject:
    filename: str
    metadata: ObjectMetadata


class UploadsStorage:
    """Narrow uploads-domain facade over :class:`ObjectStorage`."""

    def __init__(self, storage: ObjectStorage, *, namespace: ObjectKeyNamespace, user_id: str, thread_id: str) -> None:
        self._storage = storage
        self._namespace = namespace
        self._user_id = user_id
        self._thread_id = thread_id

    @classmethod
    def from_app_config(cls, app_config, *, user_id: str, thread_id: str) -> UploadsStorage:
        return cls(
            get_object_storage(app_config),
            namespace=ObjectKeyNamespace(app_config.object_storage.key_prefix),
            user_id=user_id,
            thread_id=thread_id,
        )

    @property
    def prefix(self) -> str:
        return self._namespace.uploads_prefix(self._user_id, self._thread_id)

    def _key(self, filename: str) -> str:
        if len(PurePosixPath(filename).parts) != 1:
            raise UploadStorageError("Upload objects must use a filename, not a nested path")
        try:
            return self._namespace.upload(self._user_id, self._thread_id, filename)
        except ValueError as exc:
            raise UploadStorageError("Upload filename must stay below the uploads namespace") from exc

    def virtual_path(self, filename: str) -> str:
        self._key(filename)
        return f"{UPLOADS_VIRTUAL_PREFIX}/{filename}"

    async def write_stream(
        self,
        filename: str,
        chunks: AsyncIterable[bytes],
        *,
        content_type: str | None = None,
    ) -> ObjectMetadata:
        return await self._storage.write_stream(self._key(filename), chunks, content_type=content_type)

    async def write_file(self, filename: str, path: Path, *, content_type: str | None = None) -> ObjectMetadata:
        return await self.write_stream(filename, _file_chunks(path), content_type=content_type)

    async def stat(self, filename: str) -> ObjectMetadata:
        return await self._storage.stat(self._key(filename))

    def open_read(self, filename: str):
        return self._storage.open_read(self._key(filename))

    async def read_bytes(self, filename: str, *, limit: int | None = None) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async with self.open_read(filename) as read:
            async for chunk in read.chunks:
                total += len(chunk)
                if limit is not None and total > limit:
                    raise UploadStorageError("Upload exceeds the permitted in-memory read size")
                chunks.append(chunk)
        return b"".join(chunks)

    async def delete(self, filename: str) -> None:
        await self._storage.delete(self._key(filename))

    async def copy_to(self, target_thread_id: str) -> None:
        """Copy this thread's uploads to an independently mutable branch."""
        target = type(self)(
            self._storage,
            namespace=self._namespace,
            user_id=self._user_id,
            thread_id=target_thread_id,
        )
        for upload in await self.list():
            await self._storage.copy(upload.metadata.key, target._key(upload.filename))

    async def delete_all(self) -> None:
        """Delete every persistent upload for this thread."""
        for upload in await self.list():
            await self._storage.delete(upload.metadata.key)

    async def list(self) -> list[UploadObject]:
        prefix = self.prefix + "/"
        objects: list[UploadObject] = []
        async for metadata in self._storage.list_prefix(prefix):
            if not metadata.key.startswith(prefix):
                raise UploadStorageError("Object storage returned a key outside the uploads namespace")
            filename = metadata.key[len(prefix) :]
            self._key(filename)
            if len(PurePosixPath(filename).parts) != 1:
                raise UploadStorageError("Upload objects must use a filename, not a nested path")
            objects.append(UploadObject(filename, metadata))
        return sorted(objects, key=lambda item: item.filename)

    @asynccontextmanager
    async def materialize(self, filename: str) -> AsyncIterator[Path]:
        """Expose one upload as a request/job-scoped temporary local file."""
        self._key(filename)
        with tempfile.TemporaryDirectory(prefix="deerflow-upload-") as directory:
            path = Path(directory) / filename
            async with self.open_read(filename) as read:
                handle = await asyncio.to_thread(path.open, "wb")
                try:
                    async for chunk in read.chunks:
                        await asyncio.to_thread(handle.write, chunk)
                finally:
                    await asyncio.to_thread(handle.close)
            yield path

    async def hydrate_projection(self, directory: Path) -> list[UploadObject]:
        """Replace a sandbox-local, disposable uploads projection."""
        await asyncio.to_thread(_reset_projection, directory)
        uploads = await self.list()
        for upload in uploads:
            target = directory / upload.filename
            async with self.open_read(upload.filename) as read:
                handle = await asyncio.to_thread(target.open, "wb")
                try:
                    async for chunk in read.chunks:
                        await asyncio.to_thread(handle.write, chunk)
                finally:
                    await asyncio.to_thread(handle.close)
        return uploads


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while chunk := await asyncio.to_thread(handle.read, _CHUNK_SIZE):
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


def _reset_projection(directory: Path) -> None:
    if directory.is_symlink():
        raise UploadStorageError("Uploads projection directory must not be a symlink")
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=False)
