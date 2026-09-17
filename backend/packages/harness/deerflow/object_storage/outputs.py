"""Thread outputs adapter backed exclusively by the shared object store.

The sandbox still needs a filesystem directory while a run executes.  That
directory is a disposable projection: it is hydrated from this adapter before a
run and committed back afterwards.  API consumers must use this adapter rather
than treating the projection as persistent storage.
"""

from __future__ import annotations

import asyncio
import mimetypes
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from deerflow.object_storage.keys import ObjectKeyNamespace
from deerflow.object_storage.port import ByteRange, ObjectMetadata, ObjectStorage, get_object_storage

OUTPUTS_VIRTUAL_PREFIX = "/mnt/user-data/outputs"
_CHUNK_SIZE = 64 * 1024


class OutputStorageError(RuntimeError):
    """Raised when an output cannot safely be projected or persisted."""


@dataclass(frozen=True)
class OutputObject:
    relative_path: str
    metadata: ObjectMetadata


class OutputsStorage:
    """Narrow outputs-domain facade over :class:`ObjectStorage`."""

    def __init__(self, storage: ObjectStorage, *, namespace: ObjectKeyNamespace, user_id: str, thread_id: str) -> None:
        self._storage = storage
        self._namespace = namespace
        self._user_id = user_id
        self._thread_id = thread_id

    @classmethod
    def from_app_config(cls, app_config, *, user_id: str, thread_id: str) -> OutputsStorage:
        return cls(
            get_object_storage(app_config),
            namespace=ObjectKeyNamespace(app_config.object_storage.key_prefix),
            user_id=user_id,
            thread_id=thread_id,
        )

    @property
    def prefix(self) -> str:
        return self._namespace.outputs_prefix(self._user_id, self._thread_id)

    def relative_path(self, path: str) -> str:
        normalized = path.replace("\\", "/")
        prefix = OUTPUTS_VIRTUAL_PREFIX.rstrip("/")
        if normalized == prefix or not normalized.startswith(prefix + "/"):
            raise OutputStorageError(f"Only files below {OUTPUTS_VIRTUAL_PREFIX} are valid outputs")
        relative = normalized[len(prefix) + 1 :]
        try:
            # ObjectKeyNamespace owns the traversal validation used for every
            # object-store domain, including paths supplied by HTTP callers.
            self._namespace.artifact(self._user_id, self._thread_id, relative)
        except ValueError as exc:
            raise OutputStorageError("Output path must stay below the outputs namespace") from exc
        return PurePosixPath(relative).as_posix()

    def virtual_path(self, relative_path: str) -> str:
        self._key(relative_path)
        return f"{OUTPUTS_VIRTUAL_PREFIX}/{PurePosixPath(relative_path).as_posix()}"

    def _key(self, relative_path: str) -> str:
        try:
            return self._namespace.artifact(self._user_id, self._thread_id, relative_path)
        except ValueError as exc:
            raise OutputStorageError("Output path must stay below the outputs namespace") from exc

    async def stat(self, relative_path: str) -> ObjectMetadata:
        return await self._storage.stat(self._key(relative_path))

    def open_read(self, relative_path: str, *, byte_range: ByteRange | None = None):
        return self._storage.open_read(self._key(relative_path), byte_range=byte_range)

    async def write_bytes(self, relative_path: str, content: bytes, *, content_type: str | None = None) -> ObjectMetadata:
        async def chunks() -> AsyncIterator[bytes]:
            yield content

        return await self._storage.write_stream(self._key(relative_path), chunks(), content_type=content_type)

    async def delete(self, relative_path: str) -> None:
        await self._storage.delete(self._key(relative_path))

    async def copy_to(self, target_thread_id: str) -> None:
        """Copy this thread's complete outputs namespace to a branch.

        ``.tool-results`` deliberately comes along with ordinary outputs: it is
        an internal directory in the same source-of-truth namespace, not a
        separate persistent data type.
        """
        target = type(self)(
            self._storage,
            namespace=self._namespace,
            user_id=self._user_id,
            thread_id=target_thread_id,
        )
        for output in await self.list():
            await self._storage.copy(output.metadata.key, target._key(output.relative_path))

    async def delete_all(self) -> None:
        """Delete every persistent output for this thread, including tool results."""
        for output in await self.list():
            await self._storage.delete(output.metadata.key)

    async def read_bytes(self, relative_path: str, *, limit: int | None = None) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async with self.open_read(relative_path) as read:
            async for chunk in read.chunks:
                total += len(chunk)
                if limit is not None and total > limit:
                    raise OutputStorageError("Output exceeds the permitted in-memory read size")
                chunks.append(chunk)
        return b"".join(chunks)

    async def list(self) -> list[OutputObject]:
        prefix = self.prefix + "/"
        objects: list[OutputObject] = []
        async for metadata in self._storage.list_prefix(prefix):
            if not metadata.key.startswith(prefix):
                raise OutputStorageError("Object storage returned a key outside the outputs namespace")
            relative = metadata.key[len(prefix) :]
            self._key(relative)
            objects.append(OutputObject(relative, metadata))
        return sorted(objects, key=lambda item: item.relative_path)

    async def hydrate_projection(self, directory: Path) -> list[OutputObject]:
        """Replace a local sandbox cache from the object-store source of truth."""
        await asyncio.to_thread(_reset_projection, directory)
        objects = await self.list()
        for output in objects:
            target = directory / PurePosixPath(output.relative_path)
            await asyncio.to_thread(_prepare_projection_target, directory, target)
            async with self.open_read(output.relative_path) as read:
                handle = await asyncio.to_thread(target.open, "wb")
                try:
                    async for chunk in read.chunks:
                        await asyncio.to_thread(handle.write, chunk)
                finally:
                    await asyncio.to_thread(handle.close)
        return objects

    async def commit_projection(self, directory: Path, *, protected_prefixes: tuple[str, ...] = ()) -> list[OutputObject]:
        """Commit a sandbox cache and remove objects absent from that cache."""
        files = await asyncio.to_thread(_projection_files, directory)
        existing = {output.relative_path for output in await self.list()}
        for relative_path, source in files:
            content_type, _ = mimetypes.guess_type(relative_path)
            await self._storage.write_stream(self._key(relative_path), _file_chunks(source), content_type=content_type)
        for removed in existing - {relative for relative, _ in files}:
            if any(removed.startswith(prefix) for prefix in protected_prefixes):
                continue
            await self._storage.delete(self._key(removed))
        return await self.list()

    @staticmethod
    def changed_paths(
        before: list[OutputObject],
        after: list[OutputObject],
        *,
        excluded_dir_names: frozenset[str] = frozenset(),
    ) -> list[str]:
        baseline = {item.relative_path: _version(item.metadata) for item in before}
        return [f"{OUTPUTS_VIRTUAL_PREFIX}/{item.relative_path}" for item in after if baseline.get(item.relative_path) != _version(item.metadata) and PurePosixPath(item.relative_path).parts[0] not in excluded_dir_names]


def _version(metadata: ObjectMetadata) -> tuple[str | None, int | None, str | None]:
    return metadata.etag, metadata.size, metadata.checksum_sha256


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while chunk := await asyncio.to_thread(handle.read, _CHUNK_SIZE):
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


def _reset_projection(directory: Path) -> None:
    if directory.is_symlink():
        raise OutputStorageError("Outputs projection directory must not be a symlink")
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=False)


def _prepare_projection_target(root: Path, target: Path) -> None:
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise OutputStorageError("Output projection escaped its root") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    if any(parent.is_symlink() for parent in (target.parent, *target.parent.parents) if parent != root.parent):
        raise OutputStorageError("Output projection contains a symlink")


def _projection_files(directory: Path) -> list[tuple[str, Path]]:
    if directory.is_symlink():
        raise OutputStorageError("Outputs projection directory must not be a symlink")
    if not directory.exists():
        return []
    files: list[tuple[str, Path]] = []
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise OutputStorageError("Outputs projection must not contain symlinks")
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            files.append((relative, path))
    return files
