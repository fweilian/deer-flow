"""Async, streaming S3-compatible object-storage port.

This module intentionally models only the primitives Phase 5 migrations need.
It neither knows about artifacts/uploads/skills nor offers a local-disk backend.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiobotocore.session import AioSession, get_session
from botocore.config import Config

from deerflow.config.object_storage_config import ObjectStorageConfig

_MIN_MULTIPART_PART_SIZE = 5 * 1024 * 1024
_READ_CHUNK_SIZE = 64 * 1024


class ObjectStorageConfigurationError(RuntimeError):
    """Raised when code requests shared storage without a usable configuration."""


@dataclass(frozen=True)
class ByteRange:
    """An inclusive byte range as used by HTTP and S3 Range headers."""

    start: int
    end: int | None = None

    def __post_init__(self) -> None:
        if self.start < 0 or (self.end is not None and self.end < self.start):
            raise ValueError("ByteRange must have a non-negative start and an end no smaller than start.")

    def to_header(self) -> str:
        return f"bytes={self.start}-" if self.end is None else f"bytes={self.start}-{self.end}"


@dataclass(frozen=True)
class ObjectMetadata:
    """S3 object metadata; ETags are opaque and must not be treated as MD5 hashes."""

    key: str
    size: int | None
    etag: str | None
    content_type: str | None
    last_modified: datetime | None
    checksum_sha256: str | None
    user_metadata: Mapping[str, str]


@dataclass
class ObjectRead:
    """A metadata envelope and a one-pass asynchronous byte stream."""

    metadata: ObjectMetadata
    chunks: AsyncIterator[bytes]


class ObjectStorage(ABC):
    """Narrow port for the Phase 5 object-store source of truth."""

    @abstractmethod
    async def write_stream(
        self,
        key: str,
        chunks: AsyncIterable[bytes],
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectMetadata:
        """Write one object from an asynchronous stream."""

    @abstractmethod
    def open_read(self, key: str, *, byte_range: ByteRange | None = None) -> AbstractAsyncContextManager[ObjectRead]:
        """Open a streaming object reader."""

    @abstractmethod
    async def stat(self, key: str) -> ObjectMetadata:
        """Return object metadata without reading its bytes."""

    @abstractmethod
    def list_prefix(self, prefix: str) -> AsyncIterator[ObjectMetadata]:
        """List object metadata below a stable prefix."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete one object."""

    @abstractmethod
    async def copy(self, source_key: str, destination_key: str) -> ObjectMetadata:
        """Copy one object within the configured bucket."""


class S3ObjectStorage(ObjectStorage):
    """``aiobotocore`` implementation with bounded-memory streaming writes."""

    def __init__(
        self,
        config: ObjectStorageConfig,
        *,
        session: AioSession | None = None,
        client_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
    ) -> None:
        if not config.enabled or not config.bucket:
            raise ObjectStorageConfigurationError("Shared object storage is disabled or missing its bucket configuration.")
        self._config = config
        self._session = session or get_session()
        self._client_factory = client_factory

    def _client(self) -> AbstractAsyncContextManager[Any]:
        if self._client_factory is not None:
            return self._client_factory()
        return self._session.create_client(
            "s3",
            endpoint_url=self._config.endpoint_url,
            region_name=self._config.region,
            aws_access_key_id=self._config.access_key_id,
            aws_secret_access_key=self._config.secret_access_key,
            verify=self._config.verify_tls,
            config=Config(s3={"addressing_style": "path" if self._config.force_path_style else "virtual"}),
        )

    async def write_stream(
        self,
        key: str,
        chunks: AsyncIterable[bytes],
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectMetadata:
        """Write one stream using multipart upload without a local staging file.

        S3 requires non-final multipart parts to be at least 5 MiB.  The only
        buffering here is that one bounded part; no complete object is loaded
        into memory or written to a persistent local path.
        """
        create_args = self._write_args(key, content_type, metadata)
        buffer = bytearray()
        total_size = 0
        upload_id: str | None = None
        completed = False
        parts: list[dict[str, Any]] = []

        async with self._client() as client:
            try:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("Object storage streams must yield bytes.")
                    total_size += len(chunk)
                    buffer.extend(chunk)
                    while len(buffer) >= _MIN_MULTIPART_PART_SIZE:
                        part = bytes(buffer[:_MIN_MULTIPART_PART_SIZE])
                        del buffer[:_MIN_MULTIPART_PART_SIZE]
                        if upload_id is None:
                            started = await client.create_multipart_upload(**create_args)
                            upload_id = started["UploadId"]
                        parts.append(await self._upload_part(client, key, upload_id, len(parts) + 1, part))

                if upload_id is None:
                    response = await client.put_object(**create_args, Body=bytes(buffer))
                    completed = True
                    return self._metadata_from_response(key, response, fallback_size=len(buffer), content_type=content_type, metadata=metadata)

                if buffer:
                    parts.append(await self._upload_part(client, key, upload_id, len(parts) + 1, bytes(buffer)))
                response = await client.complete_multipart_upload(
                    Bucket=self._config.bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
                completed = True
                return self._metadata_from_response(key, response, fallback_size=total_size, content_type=content_type, metadata=metadata)
            finally:
                if upload_id is not None and not completed:
                    await client.abort_multipart_upload(Bucket=self._config.bucket, Key=key, UploadId=upload_id)

    @asynccontextmanager
    async def open_read(self, key: str, *, byte_range: ByteRange | None = None) -> AsyncIterator[ObjectRead]:
        request: dict[str, Any] = {"Bucket": self._config.bucket, "Key": key}
        if byte_range is not None:
            request["Range"] = byte_range.to_header()
        async with self._client() as client:
            response = await client.get_object(**request)
            body = response["Body"]
            closed = False

            async def close_body() -> None:
                nonlocal closed
                if closed:
                    return
                closed = True
                result = body.close()
                if inspect.isawaitable(result):
                    await result

            async def stream() -> AsyncIterator[bytes]:
                try:
                    async for chunk in body.iter_chunks(chunk_size=_READ_CHUNK_SIZE):
                        if chunk:
                            yield chunk
                finally:
                    await close_body()

            try:
                yield ObjectRead(metadata=self._metadata_from_response(key, response), chunks=stream())
            finally:
                await close_body()

    async def stat(self, key: str) -> ObjectMetadata:
        async with self._client() as client:
            return self._metadata_from_response(key, await client.head_object(Bucket=self._config.bucket, Key=key))

    async def list_prefix(self, prefix: str) -> AsyncIterator[ObjectMetadata]:
        async with self._client() as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._config.bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    yield self._metadata_from_response(item["Key"], item)

    async def delete(self, key: str) -> None:
        async with self._client() as client:
            await client.delete_object(Bucket=self._config.bucket, Key=key)

    async def copy(self, source_key: str, destination_key: str) -> ObjectMetadata:
        async with self._client() as client:
            await client.copy_object(
                Bucket=self._config.bucket,
                Key=destination_key,
                CopySource={"Bucket": self._config.bucket, "Key": source_key},
            )
        return await self.stat(destination_key)

    def _write_args(self, key: str, content_type: str | None, metadata: Mapping[str, str] | None) -> dict[str, Any]:
        args: dict[str, Any] = {"Bucket": self._config.bucket, "Key": key, "ChecksumAlgorithm": "SHA256"}
        if content_type is not None:
            args["ContentType"] = content_type
        if metadata:
            args["Metadata"] = dict(metadata)
        return args

    async def _upload_part(self, client: Any, key: str, upload_id: str, part_number: int, body: bytes) -> dict[str, Any]:
        response = await client.upload_part(
            Bucket=self._config.bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=body,
        )
        return {"PartNumber": part_number, "ETag": response["ETag"]}

    @staticmethod
    def _metadata_from_response(
        key: str,
        response: Mapping[str, Any],
        *,
        fallback_size: int | None = None,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectMetadata:
        copy_result = response.get("CopyObjectResult") or {}
        return ObjectMetadata(
            key=key,
            size=response.get("ContentLength", response.get("Size", fallback_size)),
            etag=response.get("ETag", copy_result.get("ETag")),
            content_type=response.get("ContentType", content_type),
            last_modified=response.get("LastModified", copy_result.get("LastModified")),
            checksum_sha256=response.get("ChecksumSHA256"),
            user_metadata=response.get("Metadata", metadata or {}),
        )


def get_object_storage(app_config: Any | None = None) -> S3ObjectStorage:
    """Construct the configured shared store, failing closed when unavailable."""
    if app_config is None:
        from deerflow.config import get_app_config

        app_config = get_app_config()
    return S3ObjectStorage(app_config.object_storage)
