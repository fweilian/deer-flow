"""Upload router for handling file uploads."""

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.gateway.authz import require_permission
from app.gateway.deps import get_config
from deerflow.config.app_config import AppConfig
from deerflow.config.paths import get_paths
from deerflow.object_storage import UploadObject, UploadsStorage
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.uploads.manager import (
    claim_unique_filename,
    normalize_filename,
    upload_artifact_url,
    upload_virtual_path,
)
from deerflow.utils.file_conversion import CONVERTIBLE_EXTENSIONS, convert_file_to_markdown
from deerflow.utils.thread_id import ThreadId

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/threads/{thread_id}/uploads", tags=["uploads"])

UPLOAD_CHUNK_SIZE = 8192
DEFAULT_MAX_FILES = 10
DEFAULT_MAX_FILE_SIZE = 50 * 1024 * 1024
DEFAULT_MAX_TOTAL_SIZE = 100 * 1024 * 1024


class UploadedFileInfo(BaseModel):
    """Uploaded file metadata exposed by upload and list APIs."""

    filename: str
    size: int
    path: str
    virtual_path: str
    artifact_url: str
    extension: str | None = None
    modified: float | None = None
    original_filename: str | None = None
    markdown_file: str | None = None
    markdown_path: str | None = None
    markdown_virtual_path: str | None = None
    markdown_artifact_url: str | None = None


class UploadResponse(BaseModel):
    """Response model for file upload."""

    success: bool
    files: list[UploadedFileInfo]
    message: str
    skipped_files: list[str] = Field(default_factory=list)


class UploadListResponse(BaseModel):
    """Response model for uploaded file listing."""

    files: list[UploadedFileInfo]
    count: int


class UploadLimits(BaseModel):
    """Application-level upload limits exposed to clients."""

    max_files: int
    max_file_size: int
    max_total_size: int


def _get_uploads_config_value(app_config: AppConfig, key: str, default: object) -> object:
    """Read a value from the uploads config, supporting dict and attribute access."""
    uploads_cfg = getattr(app_config, "uploads", None)
    if isinstance(uploads_cfg, dict):
        return uploads_cfg.get(key, default)
    return getattr(uploads_cfg, key, default)


def _get_upload_limit(app_config: AppConfig, key: str, default: int, *, legacy_key: str | None = None) -> int:
    try:
        value = _get_uploads_config_value(app_config, key, None)
        if value is None and legacy_key is not None:
            value = _get_uploads_config_value(app_config, legacy_key, None)
        if value is None:
            value = default
        limit = int(value)
        if limit <= 0:
            raise ValueError
        return limit
    except Exception:
        logger.warning("Invalid uploads.%s value; falling back to %d", key, default)
        return default


def _get_upload_limits(app_config: AppConfig) -> UploadLimits:
    return UploadLimits(
        max_files=_get_upload_limit(app_config, "max_files", DEFAULT_MAX_FILES, legacy_key="max_file_count"),
        max_file_size=_get_upload_limit(app_config, "max_file_size", DEFAULT_MAX_FILE_SIZE, legacy_key="max_single_file_size"),
        max_total_size=_get_upload_limit(app_config, "max_total_size", DEFAULT_MAX_TOTAL_SIZE),
    )


class _LimitedUploadStream:
    """Request-body stream that enforces limits while S3 consumes it."""

    def __init__(self, file: UploadFile, *, filename: str, limits: UploadLimits, total_size: list[int]) -> None:
        self._file = file
        self._filename = filename
        self._limits = limits
        self._total_size = total_size
        self.file_size = 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[bytes]:
        while chunk := await self._file.read(UPLOAD_CHUNK_SIZE):
            self.file_size += len(chunk)
            self._total_size[0] += len(chunk)
            if self.file_size > self._limits.max_file_size:
                raise HTTPException(status_code=413, detail=f"File too large: {self._filename}")
            if self._total_size[0] > self._limits.max_total_size:
                raise HTTPException(status_code=413, detail="Total upload size too large")
            yield chunk


async def _cleanup_uploaded_objects(storage: UploadsStorage, filenames: list[str]) -> None:
    for filename in reversed(filenames):
        try:
            await storage.delete(filename)
        except Exception:
            logger.warning("Failed to clean up upload object after rejected request: %s", filename, exc_info=True)


async def _response_path(thread_id: str, user_id: str, filename: str) -> str:
    """Preserve the API's sandbox-projection path without blocking the loop."""
    directory = await asyncio.to_thread(_projection_upload_dir, thread_id, user_id)
    return str(directory / filename)


def _projection_upload_dir(thread_id: str, user_id: str):
    return get_paths().sandbox_uploads_dir(thread_id, user_id=user_id)


async def _upload_info(thread_id: str, user_id: str, filename: str, size: int, *, modified: float | None = None) -> dict:
    return {
        "filename": filename,
        "size": size,
        "path": await _response_path(thread_id, user_id, filename),
        "virtual_path": upload_virtual_path(filename),
        "artifact_url": upload_artifact_url(thread_id, filename),
        "extension": Path(filename).suffix,
        "modified": modified,
    }


def _is_missing_object_error(error: Exception) -> bool:
    if isinstance(error, FileNotFoundError):
        return True
    response = getattr(error, "response", None)
    code = str((response or {}).get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


async def _list_upload_objects(storage: UploadsStorage, thread_id: str, user_id: str) -> dict:
    uploads: list[UploadObject] = await storage.list()
    files = []
    for upload in uploads:
        files.append(
            await _upload_info(
                thread_id,
                user_id,
                upload.filename,
                upload.metadata.size or 0,
                modified=upload.metadata.last_modified.timestamp() if upload.metadata.last_modified is not None else None,
            )
        )
    return {"files": files, "count": len(files)}


def _auto_convert_documents_enabled(app_config: AppConfig) -> bool:
    """Return whether automatic host-side document conversion is enabled.

    The secure default is disabled unless an operator explicitly opts in via
    uploads.auto_convert_documents in config.yaml.
    """
    try:
        raw = _get_uploads_config_value(app_config, "auto_convert_documents", False)
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)
    except Exception:
        return False


@router.post("", response_model=UploadResponse)
@require_permission("threads", "write", owner_check=True, require_existing=False)
async def upload_files(
    thread_id: ThreadId,
    request: Request,
    files: list[UploadFile] = File(...),
    config: AppConfig = Depends(get_config),
) -> UploadResponse:
    """Stream request bodies directly into the shared uploads source of truth."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    limits = _get_upload_limits(config)
    if len(files) > limits.max_files:
        raise HTTPException(status_code=413, detail=f"Too many files: maximum is {limits.max_files}")

    effective_user_id = get_effective_user_id()
    try:
        storage = UploadsStorage.from_app_config(config, user_id=effective_user_id, thread_id=str(thread_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Upload object storage is unavailable")
        raise HTTPException(status_code=503, detail="Upload storage is unavailable") from exc

    uploaded_files: list[dict] = []
    written_filenames: list[str] = []
    skipped_files = []
    total_size = [0]
    # Track filenames within this request so duplicate form parts do not
    # silently truncate each other. Existing uploads keep the historical
    # overwrite behavior for a single replacement upload.
    seen_filenames: set[str] = set()

    auto_convert_documents = _auto_convert_documents_enabled(config)
    for file in files:
        if not file.filename:
            continue
        try:
            original_filename = normalize_filename(file.filename)
            safe_filename = claim_unique_filename(original_filename, seen_filenames)
        except ValueError:
            logger.warning("Skipping file with unsafe filename: %r", file.filename)
            continue

        try:
            body = _LimitedUploadStream(file, filename=safe_filename, limits=limits, total_size=total_size)
            content_type = getattr(file, "content_type", None)
            await storage.write_stream(safe_filename, body, content_type=content_type)
            written_filenames.append(safe_filename)
            file_info = await _upload_info(str(thread_id), effective_user_id, safe_filename, body.file_size)
            if safe_filename != original_filename:
                file_info["original_filename"] = original_filename

            if auto_convert_documents and Path(safe_filename).suffix.lower() in CONVERTIBLE_EXTENSIONS:
                provisional_md_name = Path(safe_filename).with_suffix(".md").name
                unique_md_name = claim_unique_filename(provisional_md_name, seen_filenames)
                async with storage.materialize(safe_filename) as source_path:
                    markdown_path = await convert_file_to_markdown(source_path, output_path=source_path.with_name(unique_md_name))
                    if markdown_path:
                        await storage.write_file(markdown_path.name, markdown_path, content_type="text/markdown")
                        written_filenames.append(markdown_path.name)
                        file_info["markdown_file"] = markdown_path.name
                        file_info["markdown_path"] = await _response_path(str(thread_id), effective_user_id, markdown_path.name)
                        file_info["markdown_virtual_path"] = upload_virtual_path(markdown_path.name)
                        file_info["markdown_artifact_url"] = upload_artifact_url(str(thread_id), markdown_path.name)
                    else:
                        seen_filenames.discard(unique_md_name)

            uploaded_files.append(file_info)
            logger.info("Stored upload %s (%d bytes) in shared object storage", safe_filename, body.file_size)
        except HTTPException:
            await _cleanup_uploaded_objects(storage, written_filenames)
            raise
        except Exception as exc:
            logger.exception("Failed to upload %s", file.filename)
            await _cleanup_uploaded_objects(storage, written_filenames)
            raise HTTPException(status_code=503, detail="Upload storage is unavailable") from exc

    message = f"Successfully uploaded {len(uploaded_files)} file(s)"
    if skipped_files:
        message += f"; skipped {len(skipped_files)} unsafe file(s)"
    return UploadResponse(success=not skipped_files, files=uploaded_files, message=message, skipped_files=skipped_files)


@router.get("/limits", response_model=UploadLimits)
@require_permission("threads", "read", owner_check=True)
async def get_upload_limits(
    thread_id: ThreadId,
    request: Request,
    config: AppConfig = Depends(get_config),
) -> UploadLimits:
    """Return upload limits used by the gateway for this thread."""
    return _get_upload_limits(config)


@router.get("/list", response_model=UploadListResponse)
@require_permission("threads", "read", owner_check=True)
async def list_uploaded_files(
    thread_id: ThreadId,
    request: Request,
    config: AppConfig = Depends(get_config),
) -> UploadListResponse:
    """List all files from the shared uploads source of truth."""
    try:
        user_id = get_effective_user_id()
        storage = UploadsStorage.from_app_config(config, user_id=user_id, thread_id=str(thread_id))
        result = await _list_upload_objects(storage, str(thread_id), user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as exc:
        logger.exception("Failed to list uploads for thread %s", thread_id)
        raise HTTPException(status_code=503, detail="Upload storage is unavailable") from exc

    return UploadListResponse(**result)


@router.delete("/{filename}")
@require_permission("threads", "delete", owner_check=True, require_existing=True)
async def delete_uploaded_file(
    thread_id: ThreadId,
    filename: str,
    request: Request,
    config: AppConfig = Depends(get_config),
) -> dict:
    """Delete an upload object and its generated markdown companion."""
    try:
        if Path(filename).name != filename:
            raise ValueError("Invalid path")
        filename = normalize_filename(filename)
        user_id = get_effective_user_id()
        storage = UploadsStorage.from_app_config(config, user_id=user_id, thread_id=str(thread_id))
        try:
            await storage.stat(filename)
        except Exception as exc:
            if _is_missing_object_error(exc):
                raise FileNotFoundError(filename) from exc
            raise
        await storage.delete(filename)
        if Path(filename).suffix.lower() in CONVERTIBLE_EXTENSIONS:
            await storage.delete(Path(filename).with_suffix(".md").name)
        return {"success": True, "message": f"Deleted {filename}"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as exc:
        logger.exception("Failed to delete %s", filename)
        raise HTTPException(status_code=503, detail="Upload storage is unavailable") from exc
