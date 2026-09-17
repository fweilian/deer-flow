"""Tool for discovering historical uploaded files in the current thread.

Unlike ``<current_uploads>`` which lists only this run's newly uploaded files,
this tool lets the agent discover files uploaded in previous turns on demand.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import Annotated

from langchain.tools import tool
from langgraph.config import get_config

from deerflow.agents.middlewares.input_sanitization_middleware import neutralize_untrusted_tags
from deerflow.object_storage import UploadObject, UploadsStorage
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.tools.types import Runtime
from deerflow.utils.file_outline import extract_outline_for_file

_DEFAULT_MAX_RESULTS = 20
_MAX_MAX_RESULTS = 100


def _extension_label(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    return neutralize_untrusted_tags(suffix) or "(no extension)"


def _format_omitted_summary(omitted: list[str]) -> str:
    counts = Counter(_extension_label(Path(f)) for f in omitted)
    parts = [f"{count} {ext}" for ext, count in sorted(counts.items())]
    return neutralize_untrusted_tags(", ".join(parts))


def _resolve_thread_id(runtime: Runtime) -> str | None:
    """Resolve the current thread id from runtime context or RunnableConfig."""
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id:
        return thread_id

    runtime_config = getattr(runtime, "config", None) or {}
    thread_id = runtime_config.get("configurable", {}).get("thread_id")
    if thread_id:
        return thread_id

    try:
        return get_config().get("configurable", {}).get("thread_id")
    except RuntimeError:
        return None


def _resolve_user_id(runtime: Runtime) -> str:
    """Resolve the current user id."""
    from deerflow.runtime.user_context import resolve_runtime_user_id

    return resolve_runtime_user_id(runtime) or get_effective_user_id()


def _normalize_query(query: str | None) -> str | None:
    """Return a stripped query, or None when filtering should be skipped."""
    if not isinstance(query, str):
        return None
    stripped = query.strip()
    return stripped or None


def _normalize_extensions(extensions: list[str] | None) -> frozenset[str] | None:
    """Normalize extension tokens to lowercase dotted suffixes.

    Non-strings, blanks, and a non-list input are dropped. A leading ``*`` is
    stripped so model-supplied glob tokens like ``*.pdf`` still match
    ``Path.suffix``. An empty result means "no extension filter", matching
    the unfiltered historical behavior.
    """
    if not isinstance(extensions, list):
        return None
    normalized: set[str] = set()
    for item in extensions:
        if not isinstance(item, str):
            continue
        token = item.strip().lower().lstrip("*")
        if not token:
            continue
        if not token.startswith("."):
            token = f".{token}"
        normalized.add(token)
    return frozenset(normalized) or None


def _matches_filters(
    filename: str,
    suffix: str,
    query: str | None,
    extensions: frozenset[str] | None,
) -> bool:
    if extensions is not None and suffix.lower() not in extensions:
        return False
    if query is not None and query.casefold() not in filename.casefold():
        return False
    return True


async def _list_uploaded_files_shared_impl(
    include_outline: bool | list[str] = False,
    max_results: int = _DEFAULT_MAX_RESULTS,
    runtime: Runtime | None = None,
    *,
    query: str | None = None,
    extensions: list[str] | None = None,
) -> dict:
    """List historical uploads from the shared source of truth."""
    if runtime is None:
        return {"files": [], "message": "No runtime context available."}
    thread_id = _resolve_thread_id(runtime)
    if thread_id is None:
        return {"files": [], "message": "Thread not found."}

    from deerflow.config import get_app_config

    uploads = UploadsStorage.from_app_config(get_app_config(), user_id=_resolve_user_id(runtime), thread_id=thread_id)
    current_run_filenames: set[str] = set()
    state = runtime.state
    uploaded = state.get("uploaded_files") if isinstance(state, dict) else getattr(state, "uploaded_files", None)
    if isinstance(uploaded, list):
        current_run_filenames = {entry["filename"] for entry in uploaded if isinstance(entry, dict) and isinstance(entry.get("filename"), str)}

    objects = [item for item in await uploads.list() if item.filename not in current_run_filenames]
    all_names = {item.filename for item in objects}
    candidates: list[UploadObject] = []
    for item in objects:
        if item.filename.endswith(".md") and any(name != item.filename and Path(name).stem == Path(item.filename).stem for name in all_names):
            continue
        candidates.append(item)

    query_filter = _normalize_query(query)
    extension_filter = _normalize_extensions(extensions)
    if query_filter is not None or extension_filter is not None:
        candidates = [item for item in candidates if _matches_filters(item.filename, Path(item.filename).suffix, query_filter, extension_filter)]
        if not candidates:
            return {"files": [], "total_count": 0, "message": "No uploaded files matched the given filters."}

    candidates.sort(key=lambda item: item.metadata.last_modified.timestamp() if item.metadata.last_modified is not None else 0, reverse=True)
    max_results = max(1, min(max_results, _MAX_MAX_RESULTS))
    total_count = len(candidates)
    visible = candidates[:max_results]
    omitted = [item.filename for item in candidates[max_results:]]
    if isinstance(include_outline, bool):
        outline_for_all, outline_filenames = include_outline, set()
    else:
        outline_for_all, outline_filenames = False, set(include_outline)

    files: list[dict] = []
    for item in visible:
        filename = item.filename
        file_info: dict = {
            "filename": neutralize_untrusted_tags(filename),
            "size": item.metadata.size or 0,
            "path": neutralize_untrusted_tags(f"/mnt/user-data/uploads/{filename}"),
            "extension": neutralize_untrusted_tags(Path(filename).suffix),
        }
        if outline_for_all or filename in outline_filenames:
            async with uploads.materialize(filename) as path:
                outline, preview = await asyncio.to_thread(extract_outline_for_file, path)
            if outline:
                file_info["outline"] = [{**entry, "title": neutralize_untrusted_tags(entry["title"])} if "title" in entry else entry for entry in outline]
            if preview:
                file_info["outline_preview"] = [neutralize_untrusted_tags(item) for item in preview]
        files.append(file_info)

    result: dict = {"files": files, "total_count": total_count, "message": f"Found {total_count} historical file(s)." if files else "No historical uploaded files in this thread."}
    if total_count > max_results:
        result["truncated"] = True
        result["omitted_summary"] = _format_omitted_summary(omitted)
    return result


@tool
async def list_uploaded_files(
    runtime: Runtime,
    include_outline: Annotated[
        bool | list[str],
        "Control which files get their document outline (headings/preview) returned. "
        "False (default): no outline for any file — just filename, size, and path. "
        "True: include outline/preview for every .md-convertible file. "
        'list of filenames: include outline/preview only for those specific files (e.g. ["report.md", "data.csv"]).',
    ] = False,
    max_results: Annotated[
        int,
        "Maximum number of files to return (default 20, max 100).",
    ] = _DEFAULT_MAX_RESULTS,
    query: Annotated[
        str | None,
        "Optional case-insensitive substring to match against the filename only (not the virtual path). Omit or leave blank to skip name filtering.",
    ] = None,
    extensions: Annotated[
        list[str] | None,
        'Optional file extensions to keep, e.g. ["pdf", ".PNG"]. With or without a leading dot; matching is case-insensitive. Combined with query using AND. Omit to skip type filtering.',
    ] = None,
) -> dict:
    """Discover historical uploaded files available in this thread.

    Returns files that were uploaded in PREVIOUS turns — files uploaded in the
    current run are excluded (they are already listed in <current_uploads>).

    Use this tool when:
    - The user refers to previously uploaded files without naming them (e.g. "analyze those PDFs I uploaded before")
    - You need to check what files are available in this thread
    - You are starting work on a thread and want an overview of available data

    Skip this tool when:
    - The user names a specific file — use read_file or grep directly with the path
    - The file was uploaded in the current run — it's already in <current_uploads>

    Optional filters (`query`, `extensions`) run before the max_results cap, so
    older matching files are not displaced by newer unrelated uploads.
    """
    return await _list_uploaded_files_shared_impl(
        include_outline=include_outline,
        max_results=max_results,
        runtime=runtime,
        query=query,
        extensions=extensions,
    )
