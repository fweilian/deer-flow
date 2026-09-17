"""One-way shared-upload projection for sandboxes without thread-data mounts."""

from __future__ import annotations

import asyncio
import shlex

from deerflow.object_storage import UPLOADS_VIRTUAL_PREFIX, UploadsStorage, UploadStorageError


async def hydrate_remote_uploads(uploads: UploadsStorage, sandbox) -> None:
    """Replace a remote sandbox's disposable uploads view from shared storage."""
    result = await asyncio.to_thread(
        sandbox.execute_command,
        f"rm -rf {shlex.quote(UPLOADS_VIRTUAL_PREFIX)} && mkdir -p {shlex.quote(UPLOADS_VIRTUAL_PREFIX)}",
    )
    if isinstance(result, str) and result.lstrip().startswith("Error:"):
        raise UploadStorageError(f"Failed to reset remote uploads projection: {result}")
    for upload in await uploads.list():
        # Sandbox.update_file accepts a single bytes value. This transfers one
        # temporary projection file only; the object store remains authoritative.
        await asyncio.to_thread(sandbox.update_file, uploads.virtual_path(upload.filename), await uploads.read_bytes(upload.filename))
