"""One-way Phase 5 synchronization for sandboxes without thread-data mounts.

This is deliberately limited to the outputs namespace.  It composes the
existing sandbox file methods with ``OutputsStorage`` rather than adding a
general sandbox filesystem or storage abstraction.
"""

from __future__ import annotations

import asyncio
import posixpath
import shlex

from deerflow.object_storage import (
    OUTPUTS_VIRTUAL_PREFIX,
    OutputsStorage,
    OutputStorageError,
)

_REMOTE_OUTPUT_GLOB_LIMIT = 10_000


async def hydrate_remote_outputs(outputs: OutputsStorage, sandbox) -> None:
    """Replace a remote sandbox's disposable outputs view from shared storage."""
    await _reset_remote_outputs(sandbox)
    for output in await outputs.list():
        # The current Sandbox binary-upload contract accepts one byte string.
        # This is per-file transfer only; it does not introduce a host staging
        # path or buffer an outputs tree as a whole.
        content = await outputs.read_bytes(output.relative_path)
        virtual_path = outputs.virtual_path(output.relative_path)
        await _mkdir_remote_parent(sandbox, virtual_path)
        await asyncio.to_thread(sandbox.update_file, virtual_path, content)


async def commit_remote_outputs(
    outputs: OutputsStorage,
    sandbox,
    *,
    protected_prefixes: tuple[str, ...] = (),
) -> None:
    """Persist a remote sandbox's outputs view before its lease is released."""
    paths, truncated = await asyncio.to_thread(
        sandbox.glob,
        OUTPUTS_VIRTUAL_PREFIX,
        "**",
        include_dirs=False,
        max_results=_REMOTE_OUTPUT_GLOB_LIMIT,
    )
    if truncated:
        raise OutputStorageError("Remote sandbox outputs exceed the synchronization result limit")

    files: dict[str, str] = {}
    for virtual_path in paths:
        relative_path = outputs.relative_path(virtual_path)
        files[relative_path] = virtual_path

    existing = {output.relative_path for output in await outputs.list()}
    for relative_path, virtual_path in sorted(files.items()):
        content = await asyncio.to_thread(sandbox.download_file, virtual_path)
        await outputs.write_bytes(relative_path, content)
    for removed in existing - files.keys():
        if not any(removed.startswith(prefix) for prefix in protected_prefixes):
            await outputs.delete(removed)


async def _reset_remote_outputs(sandbox) -> None:
    result = await asyncio.to_thread(
        sandbox.execute_command,
        f"rm -rf {shlex.quote(OUTPUTS_VIRTUAL_PREFIX)} && mkdir -p {shlex.quote(OUTPUTS_VIRTUAL_PREFIX)}",
    )
    if isinstance(result, str) and result.lstrip().startswith("Error:"):
        raise OutputStorageError(f"Failed to reset remote outputs projection: {result}")


async def _mkdir_remote_parent(sandbox, virtual_path: str) -> None:
    parent = posixpath.dirname(virtual_path)
    result = await asyncio.to_thread(sandbox.execute_command, f"mkdir -p {shlex.quote(parent)}")
    if isinstance(result, str) and result.lstrip().startswith("Error:"):
        raise OutputStorageError(f"Failed to prepare remote outputs projection: {result}")
