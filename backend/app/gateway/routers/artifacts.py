import asyncio
import hashlib
import logging
import mimetypes
import os
import tempfile
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.gateway.authz import SandboxRequestLease, require_permission, try_acquire_sandbox_for_request
from app.gateway.deps import get_run_manager
from app.gateway.internal_auth import get_trusted_internal_owner_user_id
from app.gateway.path_utils import normalize_outputs_virtual_path
from deerflow.authz.sandbox_authz import safe_app_config
from deerflow.config.paths import make_safe_user_id
from deerflow.object_storage import ByteRange, OutputsStorage, OutputStorageError
from deerflow.runtime import ConflictError, ThreadOperationKind
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.sandbox.sandbox_provider import get_sandbox_provider
from deerflow.utils.thread_id import ThreadId

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["artifacts"])

# Exact matches only; ``_is_active_content_mime_type`` also treats every
# ``+xml`` subtype as active content.
ACTIVE_CONTENT_MIME_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/xml",
    "application/xml",
    "text/xsl",
}

MAX_SKILL_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024
_SKILL_ARCHIVE_READ_CHUNK_SIZE = 64 * 1024
MAX_EDITABLE_ARTIFACT_BYTES = 2 * 1024 * 1024


class ArtifactUpdateRequest(BaseModel):
    content: str
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactUpdateResponse(BaseModel):
    path: str
    sha256: str
    size: int


@asynccontextmanager
async def reserve_artifact_write(request: Request, thread_id: str, *, user_id: str) -> AsyncIterator[None]:
    """Serialize an artifact edit against runs and other thread mutations."""
    run_manager = get_run_manager(request)
    async with run_manager.reserve_thread_operation(
        thread_id,
        kind=ThreadOperationKind.artifact_write,
        user_id=user_id,
    ):
        yield


def _normalize_editable_artifact_path(path: str) -> str:
    # The object-key namespace independently validates the normalized relative
    # path, so an encoded ``..`` cannot escape the outputs object prefix.
    virtual_path = normalize_outputs_virtual_path(path)
    if ".skill/" in virtual_path or virtual_path.endswith(".skill"):
        raise HTTPException(status_code=415, detail="Skill archives cannot be edited in the artifacts panel")
    return virtual_path


def _encode_artifact_update(content: str) -> bytes:
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_EDITABLE_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="Artifact is too large to edit")
    if b"\x00" in encoded:
        raise HTTPException(status_code=415, detail="Binary content cannot be saved as an artifact")
    return encoded


def _validate_editable_object(content: bytes, path: str, expected_sha256: str) -> None:
    if len(content) > MAX_EDITABLE_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="Artifact is too large to edit")
    if b"\x00" in content:
        raise HTTPException(status_code=415, detail="Binary artifacts cannot be edited")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="Only UTF-8 text artifacts can be edited") from None
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise HTTPException(status_code=412, detail="Artifact changed since it was opened")


def _sync_artifact_to_sandbox(sandbox, virtual_path: str, content: bytes) -> None:
    sandbox.update_file(virtual_path, content)


def _build_content_disposition(disposition_type: str, filename: str) -> str:
    """Build an RFC 5987 encoded Content-Disposition header value."""
    return f"{disposition_type}; filename*=UTF-8''{quote(filename)}"


def _build_attachment_headers(filename: str, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Content-Disposition": _build_content_disposition("attachment", filename)}
    if extra_headers:
        headers.update(extra_headers)
    return headers


def _object_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    return str(error.get("Code", "")) in {"404", "NoSuchKey", "NotFound"}


def _http_range(range_header: str | None, size: int | None) -> tuple[ByteRange | None, int, dict[str, str]]:
    """Translate one HTTP byte range to the object-store port contract."""
    headers = {"Accept-Ranges": "bytes"}
    if range_header is None:
        return None, 200, headers
    if size is None or not range_header.startswith("bytes=") or "," in range_header:
        raise HTTPException(status_code=416, detail="Requested range is not satisfiable", headers={**headers, "Content-Range": f"bytes */{size or 0}"})
    start_text, separator, end_text = range_header.removeprefix("bytes=").partition("-")
    if not separator:
        raise HTTPException(status_code=416, detail="Requested range is not satisfiable", headers={**headers, "Content-Range": f"bytes */{size}"})
    try:
        if start_text:
            start = int(start_text)
            end = size - 1 if not end_text else min(int(end_text), size - 1)
        else:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            start, end = max(size - suffix, 0), size - 1
    except ValueError as exc:
        raise HTTPException(status_code=416, detail="Requested range is not satisfiable", headers={**headers, "Content-Range": f"bytes */{size}"}) from exc
    if size == 0 or start < 0 or start >= size or end < start:
        raise HTTPException(status_code=416, detail="Requested range is not satisfiable", headers={**headers, "Content-Range": f"bytes */{size}"})
    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)})
    return ByteRange(start, end), 206, headers


async def _stream_output(outputs: OutputsStorage, relative_path: str, byte_range: ByteRange | None):
    async with outputs.open_read(relative_path, byte_range=byte_range) as read:
        async for chunk in read.chunks:
            yield chunk


async def _load_skill_archive_from_outputs(outputs: OutputsStorage, relative_path: str, internal_path: str, skill_file_path: str) -> tuple[bytes, str | None]:
    """Materialize one archive only for the request-scoped ZIP reader.

    ``zipfile`` requires random access.  The temporary file is removed before
    this coroutine returns and is never an outputs source of truth.
    """
    descriptor, temporary_path = tempfile.mkstemp(prefix=".artifact-skill-", suffix=".skill")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            async with outputs.open_read(relative_path) as read:
                async for chunk in read.chunks:
                    await asyncio.to_thread(handle.write, chunk)
        return await asyncio.to_thread(_load_skill_archive_member, Path(temporary_path), skill_file_path, internal_path)
    finally:
        try:
            await asyncio.to_thread(os.unlink, temporary_path)
        except FileNotFoundError:
            pass


def _slice_byte_range(content: bytes, range_header: str | None) -> tuple[bytes, int, dict[str, str]]:
    """Apply one RFC 9110 byte range to an in-memory archive member."""
    size = len(content)
    headers = {"Accept-Ranges": "bytes"}
    if range_header is None:
        return content, 200, headers

    def unsatisfied() -> HTTPException:
        return HTTPException(
            status_code=416,
            detail="Requested range is not satisfiable",
            headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"},
        )

    if not range_header.startswith("bytes=") or "," in range_header:
        raise unsatisfied()
    range_spec = range_header.removeprefix("bytes=")
    if "-" not in range_spec:
        raise unsatisfied()
    start_text, end_text = range_spec.split("-", 1)
    try:
        if start_text:
            start = int(start_text)
            end = size - 1 if not end_text else min(int(end_text), size - 1)
        else:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise unsatisfied()
            start = max(size - suffix_length, 0)
            end = size - 1
    except ValueError as exc:
        raise unsatisfied() from exc

    if size == 0 or start < 0 or start >= size or end < start:
        raise unsatisfied()
    ranged_content = content[start : end + 1]
    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(len(ranged_content)),
        }
    )
    return ranged_content, 206, headers


def _is_active_content_mime_type(mime_type: str | None) -> bool:
    """Return whether a browser can run script when rendering *mime_type* inline.

    Beyond HTML, this covers every WHATWG XML MIME type (``text/xml``,
    ``application/xml``, or a ``+xml`` subtype) plus ``text/xsl``, which Blink
    also renders as XML: any XML document can carry an XHTML-namespaced
    ``<script>``, so ``report.xml`` or ``feed.rss`` is as dangerous as
    ``page.html`` when opened in the application origin.
    """
    if mime_type is None:
        return False
    mime_type = mime_type.lower()
    return mime_type in ACTIVE_CONTENT_MIME_TYPES or mime_type.endswith("+xml")


def _read_skill_archive_member(zip_ref: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """Read a .skill archive member while enforcing an uncompressed size cap."""
    if info.file_size > MAX_SKILL_ARCHIVE_MEMBER_BYTES:
        raise HTTPException(status_code=413, detail="Skill archive member is too large to preview")

    chunks: list[bytes] = []
    total_read = 0
    with zip_ref.open(info, "r") as src:
        while chunk := src.read(_SKILL_ARCHIVE_READ_CHUNK_SIZE):
            total_read += len(chunk)
            if total_read > MAX_SKILL_ARCHIVE_MEMBER_BYTES:
                raise HTTPException(status_code=413, detail="Skill archive member is too large to preview")
            chunks.append(chunk)
    return b"".join(chunks)


def _extract_file_from_skill_archive(zip_path: Path, internal_path: str) -> bytes | None:
    """Extract a file from a .skill ZIP archive.

    Args:
        zip_path: Path to the .skill file (ZIP archive).
        internal_path: Path to the file inside the archive (e.g., "SKILL.md").

    Returns:
        The file content as bytes, or None if not found.
    """
    if not zipfile.is_zipfile(zip_path):
        return None

    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            # List all files in the archive
            infos_by_name = {info.filename: info for info in zip_ref.infolist()}

            # Try direct path first
            if internal_path in infos_by_name:
                return _read_skill_archive_member(zip_ref, infos_by_name[internal_path])

            # Try with any top-level directory prefix (e.g., "skill-name/SKILL.md")
            for name, info in infos_by_name.items():
                if name.endswith("/" + internal_path) or name == internal_path:
                    return _read_skill_archive_member(zip_ref, info)

            # Not found
            return None
    except (zipfile.BadZipFile, KeyError):
        return None


def _load_skill_archive_member(actual_skill_path: Path, skill_file_path: str, internal_path: str) -> tuple[bytes, str | None]:
    """Worker-thread body for the ``.skill`` branch of ``get_artifact``.

    The ``exists`` / ``is_file`` probes, the ZIP open+extract, and the MIME
    sniff (``mimetypes`` lazily stats the system MIME database on first use) are
    blocking filesystem IO and must stay off the event loop. Raised
    ``HTTPException``s propagate through ``asyncio.to_thread`` unchanged,
    preserving status codes.
    """
    if not actual_skill_path.exists():
        raise HTTPException(status_code=404, detail=f"Skill file not found: {skill_file_path}")
    if not actual_skill_path.is_file():
        raise HTTPException(status_code=400, detail=f"Path is not a file: {skill_file_path}")
    content = _extract_file_from_skill_archive(actual_skill_path, internal_path)
    if content is None:
        raise HTTPException(status_code=404, detail=f"File '{internal_path}' not found in skill archive")
    mime_type, _ = mimetypes.guess_type(internal_path)
    return content, mime_type


@router.get(
    "/threads/{thread_id}/artifacts/{path:path}",
    summary="Get Artifact File",
    description="Retrieve an artifact file generated by the AI agent. Text and binary files can be viewed inline, while active web content is always downloaded.",
)
@require_permission("threads", "read", owner_check=True)
async def get_artifact(thread_id: ThreadId, path: str, request: Request, download: bool = False) -> Response:
    """Get an artifact file by its path.

    The endpoint automatically detects file types and returns appropriate content types.
    Use the `download` query parameter to force file download for non-active content.

    Args:
        thread_id: The thread ID.
        path: The artifact path with virtual prefix (e.g., mnt/user-data/outputs/file.txt).
        request: FastAPI request object (automatically injected).

    Returns:
        The file content as a FileResponse with appropriate content type:
        - Active content (HTML and XML documents, including XHTML/SVG): Served as download attachment
        - Text files: Plain text with proper MIME type
        - Binary files: Inline display with download option

    Raises:
        HTTPException:
            - 400 if path is invalid or not a file
            - 403 if access denied (path traversal detected)
            - 404 if file not found

    Query Parameters:
        download (bool): If true, forces attachment download for file types that are
            otherwise returned inline or as plain text. Active HTML/XML content
            (including XHTML and SVG) is always downloaded regardless of this flag.

    Example:
        - Get text file inline: `/api/threads/abc123/artifacts/mnt/user-data/outputs/notes.txt`
        - Download file: `/api/threads/abc123/artifacts/mnt/user-data/outputs/data.csv?download=true`
        - Active web content such as `.html`, `.xhtml`, `.svg`, and `.xml` artifacts is always downloaded
    """
    # Trusted internal callers may act on behalf of a thread's owner via the
    # owner-user-id header (honored only after the internal token validates).
    # The header carries the raw platform owner id, while runs store files
    # under the make_safe_user_id bucket (the same normalization the channel
    # file pipeline and the memory router apply), so resolution uses the
    # normalized id. Browser/API callers get None here and fall back to the
    # effective user.
    raw_owner_user_id = get_trusted_internal_owner_user_id(request)
    owner_user_id = make_safe_user_id(raw_owner_user_id) if raw_owner_user_id else None
    effective_user_id = owner_user_id or make_safe_user_id(get_effective_user_id())
    try:
        outputs = OutputsStorage.from_app_config(safe_app_config(), user_id=effective_user_id, thread_id=str(thread_id))
    except Exception as exc:
        logger.exception("Artifact object storage is unavailable")
        raise HTTPException(status_code=503, detail="Artifact storage is unavailable") from exc

    # Check if this is a request for a file inside a .skill archive (e.g., xxx.skill/SKILL.md)
    if ".skill/" in path:
        # Split the path at ".skill/" to get the ZIP file path and internal path
        skill_marker = ".skill/"
        marker_pos = path.find(skill_marker)
        skill_file_path = path[: marker_pos + len(".skill")]  # e.g., "mnt/user-data/outputs/my-skill.skill"
        internal_path = path[marker_pos + len(skill_marker) :]  # e.g., "SKILL.md"

        try:
            relative_skill_path = outputs.relative_path(normalize_outputs_virtual_path(skill_file_path))
            content, mime_type = await _load_skill_archive_from_outputs(outputs, relative_skill_path, internal_path, skill_file_path)
        except OutputStorageError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            if _object_not_found(exc):
                raise HTTPException(status_code=404, detail=f"Skill file not found: {skill_file_path}") from None
            if isinstance(exc, HTTPException):
                raise
            logger.exception("Failed to read skill archive %s", skill_file_path)
            raise HTTPException(status_code=503, detail="Artifact storage is unavailable") from exc

        # Add cache headers to avoid repeated ZIP extraction (cache for 5 minutes)
        cache_headers = {"Cache-Control": "private, max-age=300"}
        download_name = Path(internal_path).name or Path(skill_file_path).stem
        if download or _is_active_content_mime_type(mime_type):
            return Response(content=content, media_type=mime_type or "application/octet-stream", headers=_build_attachment_headers(download_name, cache_headers))

        # Archive members are already bounded during extraction. Preserve byte
        # semantics here so the frontend can request only its preview budget,
        # including a final partial UTF-8 sequence.
        request_headers = request.headers if request is not None else {}
        range_header = None if request_headers.get("if-range") else request_headers.get("range")
        ranged_content, status_code, range_headers = _slice_byte_range(content, range_header)
        inline_headers = {
            **cache_headers,
            **range_headers,
            # Real SHA-256 so the browser can skip crypto.subtle (unavailable on
            # non-secure contexts) when previewing / editing artifacts (#4864).
            "ETag": f'"{hashlib.sha256(content).hexdigest()}"',
        }

        if mime_type and mime_type.startswith("text/"):
            return Response(content=ranged_content, status_code=status_code, media_type=mime_type, headers=inline_headers)

        # Default to plain text for unknown types that look like text
        try:
            content.decode("utf-8")
            return Response(content=ranged_content, status_code=status_code, media_type="text/plain", headers=inline_headers)
        except UnicodeDecodeError:
            return Response(
                content=ranged_content,
                status_code=status_code,
                media_type=mime_type or "application/octet-stream",
                headers=inline_headers,
            )

    try:
        virtual_path = normalize_outputs_virtual_path(path)
        relative_path = outputs.relative_path(virtual_path)
        metadata = await outputs.stat(relative_path)
    except OutputStorageError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        if _object_not_found(exc):
            raise HTTPException(status_code=404, detail=f"Artifact not found: {path}") from None
        logger.exception("Failed to stat artifact %s for thread %s", path, thread_id)
        raise HTTPException(status_code=503, detail="Artifact storage is unavailable") from exc

    request_headers = request.headers if request is not None else {}
    selected_range, status_code, range_headers = _http_range(None if request_headers.get("if-range") else request_headers.get("range"), metadata.size)
    filename = PurePosixPath(relative_path).name
    mime_type = metadata.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    disposition = "attachment" if download or _is_active_content_mime_type(mime_type) else "inline"
    headers = {"Content-Disposition": _build_content_disposition(disposition, filename), **range_headers}
    if metadata.etag:
        headers["ETag"] = metadata.etag
    elif metadata.checksum_sha256:
        headers["ETag"] = f'"{metadata.checksum_sha256}"'
    if selected_range is None and metadata.size is not None:
        headers["Content-Length"] = str(metadata.size)
    return StreamingResponse(
        _stream_output(outputs, relative_path, selected_range),
        status_code=status_code,
        media_type=mime_type,
        headers=headers,
    )


@router.put(
    "/threads/{thread_id}/artifacts/{path:path}",
    response_model=ArtifactUpdateResponse,
    summary="Update Artifact File",
    description="Replace an existing UTF-8 text artifact after verifying that its content has not changed.",
)
@require_permission("threads", "write", owner_check=True, require_existing=True)
async def update_artifact(
    thread_id: ThreadId,
    path: str,
    body: ArtifactUpdateRequest,
    request: Request,
) -> ArtifactUpdateResponse:
    """Update an existing text artifact while the thread has no active run.

    The host-side artifact file is updated first; when the sandbox provider is
    not thread-mounted, the new content is also synced into the thread's
    sandbox. Under ``authorization.enabled``, a caller denied
    ``sandbox:execute`` skips that sandbox sync (the host-side update still
    completes).
    """
    virtual_path = _normalize_editable_artifact_path(path)
    raw_owner_user_id = get_trusted_internal_owner_user_id(request)
    effective_user_id = make_safe_user_id(raw_owner_user_id) if raw_owner_user_id else get_effective_user_id()

    sandbox_lease: SandboxRequestLease | None = None
    sandbox = None
    try:
        async with reserve_artifact_write(request, thread_id, user_id=effective_user_id):
            outputs = OutputsStorage.from_app_config(safe_app_config(), user_id=effective_user_id, thread_id=str(thread_id))
            relative_path = outputs.relative_path(virtual_path)
            try:
                current = await outputs.read_bytes(relative_path, limit=MAX_EDITABLE_ARTIFACT_BYTES)
            except Exception as exc:
                if _object_not_found(exc):
                    raise HTTPException(status_code=404, detail=f"Artifact not found: {virtual_path}") from None
                raise
            _validate_editable_object(current, virtual_path, body.expected_sha256)
            updated = _encode_artifact_update(body.content)

            sandbox_provider = get_sandbox_provider()
            if not bool(getattr(sandbox_provider, "uses_thread_data_mounts", False)):
                # Phase 3: enforce sandbox:execute before acquiring — a denied
                # role skips the sandbox sync; the host-side artifact update
                # still completes (the agent cannot consume the sandbox copy
                # anyway when sandbox execution is denied).
                sandbox_lease = await try_acquire_sandbox_for_request(
                    request,
                    sandbox_provider,
                    thread_id,
                    user_id=effective_user_id,
                    app_config=safe_app_config(),
                    owner_prefix="gateway:artifact",
                )
                sandbox = sandbox_lease.sandbox
                if not sandbox_lease.denied and sandbox is None:
                    raise RuntimeError("Failed to acquire sandbox for artifact update")

            try:
                if sandbox is not None:
                    await asyncio.to_thread(_sync_artifact_to_sandbox, sandbox, virtual_path, updated)
                await outputs.write_bytes(relative_path, updated, content_type="text/plain; charset=utf-8")
            except Exception:
                if sandbox is not None:
                    try:
                        await asyncio.to_thread(_sync_artifact_to_sandbox, sandbox, virtual_path, current)
                    except Exception:
                        logger.exception("Failed to roll back remote artifact after artifact update failure: %s", virtual_path)
                raise
    except ConflictError:
        raise HTTPException(status_code=409, detail="Thread has a run in flight. Save after the run finishes.") from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to update artifact %s for thread %s", path, thread_id)
        raise HTTPException(status_code=500, detail="Failed to update artifact") from None
    finally:
        if sandbox_lease is not None:
            try:
                await sandbox_lease.release()
            except Exception:
                logger.warning(
                    "Failed to release sandbox request lease after artifact update: %s",
                    sandbox_lease.sandbox_id,
                    exc_info=True,
                )

    content_sha256 = hashlib.sha256(updated).hexdigest()
    return ArtifactUpdateResponse(
        path=virtual_path,
        sha256=content_sha256,
        size=len(updated),
    )
