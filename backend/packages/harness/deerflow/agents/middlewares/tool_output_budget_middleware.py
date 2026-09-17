"""Middleware that enforces a per-result budget on tool outputs.

Oversized tool results are persisted to shared outputs storage and replaced
with a compact typed synopsis containing a file reference.  When shared
storage is unavailable the middleware falls back to head+tail truncation so the
model context is never blown by a single large tool return.

The model-call hooks also budget the other bulky side of a tool call: the
``content`` argument of a successful ``write_file`` call (issue #5328, step
2). After a successful write the file on disk is the source of truth, and the
read-before-write gate forces a ``read_file`` before the next modification of
that path, so once a *later* successful read or write of the same path exists
the historical copy is redundant with it. Such superseded content is replaced
by a short deterministic placeholder in the *model-bound request only*
(``request.override``): ``state["messages"]``, checkpoints, tool receipts,
loop detection, and the run journal keep the original arguments, and nothing
is externalized to disk (the file itself is the reference). The newest
``keep_recent_writes`` successful writes always stay visible so the model can
still say what it just wrote without a read. Gate-blocked calls are the
read-before-write middleware's own policy; both rewrite through the shared
``tool_call_args`` helper so every provider surface changes together.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import shlex
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace as dc_replace
from typing import TYPE_CHECKING, Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.agents.middlewares.tool_call_args import ToolCallOccurrence, pair_tool_call_results, rewrite_messages_tool_call_args
from deerflow.agents.middlewares.tool_output_synopsis import render_tool_output_preview
from deerflow.agents.middlewares.tool_result_meta import TOOL_META_KEY
from deerflow.agents.middlewares.tool_transform_meta import append_tool_transform
from deerflow.config.tool_output_config import ToolOutputConfig
from deerflow.object_storage import OutputsStorage
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.sandbox.sandbox_provider import get_sandbox_provider

if TYPE_CHECKING:
    from deerflow.sandbox.sandbox import Sandbox

logger = logging.getLogger(__name__)

# Virtual outputs root inside the sandbox. Host-mounted sandboxes map this to
# the thread outputs dir on the host; for non-mounted (remote) sandboxes the
# same path is written directly into the sandbox filesystem so the model's
# ``read_file`` tool can read it back (issue #3416).
_VIRTUAL_OUTPUTS_BASE = "/mnt/user-data/outputs"


def _default_config() -> ToolOutputConfig:
    return ToolOutputConfig()


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _message_text(content: Any) -> str | None:
    """Extract a plain-text representation from a ToolMessage content field.

    Returns ``None`` for non-string / multimodal content so the caller
    can skip budget enforcement (images, structured blocks, etc.).
    """
    if isinstance(content, str):
        return content
    if content is None:
        return None
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
            else:
                return None
        return "\n".join(pieces) if pieces else None
    return None


def _snap_to_line_boundary(text: str, pos: int) -> int:
    """Return *pos* or the nearest preceding newline+1, whichever is closer.

    Used so that previews and truncations end on a complete line when
    possible.  If no newline exists in the second half of ``text[:pos]``
    the original *pos* is returned unchanged.

    Only valid for an *end* offset: moving backwards shortens the slice that
    ends here.  Use :func:`_snap_start_to_line_boundary` for a start offset.
    """
    if pos <= 0 or pos >= len(text):
        return pos
    half = pos // 2
    nl = text.rfind("\n", half, pos)
    if nl >= 0:
        return nl + 1
    return pos


def _snap_start_to_line_boundary(text: str, pos: int) -> int:
    """Return *pos* or the nearest following newline+1, whichever is closer.

    The start-offset mirror of :func:`_snap_to_line_boundary`. Snapping a start
    backwards would *lengthen* the slice beginning there, so the tail of a
    budgeted preview must snap forward instead. If no newline exists in the
    first half of ``text[pos:]`` the original *pos* is returned unchanged.
    """
    if pos <= 0 or pos >= len(text):
        return pos
    half = pos + (len(text) - pos) // 2
    nl = text.find("\n", pos, half)
    if nl >= 0:
        return nl + 1
    return pos


# ---------------------------------------------------------------------------
# Disk persistence
# ---------------------------------------------------------------------------

_EXT_MAP: dict[str, str] = {
    "bash": "log",
    "bash_tool": "log",
    "web_fetch": "log",
}


def _sanitize_tool_name(name: str) -> str:
    """Strip path separators and traversal components from a tool name."""
    base = os.path.basename(name)
    safe = base.replace("..", "").replace("/", "_").replace("\\", "_")
    return safe or "unknown"


def _build_externalized_filename(*, tool_name: str, tool_call_id: str) -> str:
    """Build the on-disk filename for an externalized tool output.

    Shared by the host-disk and sandbox externalization paths so both
    produce the identical naming scheme.
    """
    safe_name = _sanitize_tool_name(tool_name)
    ext = _EXT_MAP.get(tool_name, "txt")
    short_id = uuid.uuid4().hex[:12]
    return f"{safe_name}-{short_id}.{ext}"


# ---------------------------------------------------------------------------
# Preview / fallback builders
# ---------------------------------------------------------------------------


def _build_preview(
    content: str,
    *,
    tool_name: str,
    virtual_path: str,
    head_chars: int,
    tail_chars: int,
) -> str:
    """Build a typed synopsis preview with a file reference for externalized output."""
    return render_tool_output_preview(
        content,
        tool_name=tool_name,
        virtual_path=virtual_path,
        head_chars=head_chars,
        tail_chars=tail_chars,
    )


def _build_fallback(
    content: str,
    *,
    tool_name: str,
    max_chars: int,
    head_chars: int,
    tail_chars: int,
) -> str:
    """Build a head+tail truncation when disk persistence is unavailable.

    The returned string is guaranteed to be no longer than *max_chars*.
    """
    total = len(content)
    if max_chars <= 0 or total <= max_chars:
        return content

    marker_template = "\n\n[... {n} chars omitted from {tn} output. Persistent storage unavailable. Consider narrowing the query or using more specific parameters.]\n\n"
    marker_overhead = len(marker_template.format(n=total, tn=tool_name))

    if marker_overhead >= max_chars:
        return content[:max_chars]

    budget = max_chars - marker_overhead
    effective_head = min(head_chars, budget)
    effective_tail = min(tail_chars, max(0, budget - effective_head))

    head_end = _snap_to_line_boundary(content, min(effective_head, total))
    tail_start = _snap_start_to_line_boundary(content, max(head_end, total - effective_tail))

    head = content[:head_end]
    tail = content[tail_start:] if tail_start < total else ""
    omitted = total - len(head) - len(tail)

    marker = marker_template.format(n=omitted, tn=tool_name)

    parts = [head, marker]
    if tail:
        parts.append(tail)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Core budget logic
# ---------------------------------------------------------------------------


def _resolve_sandbox(request: ToolCallRequest) -> Sandbox | None:
    """Resolve the active sandbox for the current tool call, or ``None``.

    Reads the sandbox_id that ``SandboxMiddleware`` (and the sandbox tools
    themselves) write into ``runtime.state["sandbox"]``. We intentionally do
    NOT call ``provider.acquire`` here: acquiring a sandbox can trigger
    blocking remote I/O, and this resolver runs on every tool call. Tools
    that do not use a sandbox (``web_search``, MCP, ...) will return ``None``
    here, which is fine -- the caller falls back to inline truncation.
    """
    runtime = getattr(request, "runtime", None)
    state = getattr(runtime, "state", None)
    if not isinstance(state, dict):
        return None
    sandbox_state = state.get("sandbox")
    if not isinstance(sandbox_state, dict):
        return None
    sandbox_id = sandbox_state.get("sandbox_id")
    if not sandbox_id:
        return None
    try:
        return get_sandbox_provider().get(sandbox_id)
    except Exception:
        logger.exception("Failed to look up sandbox %s for tool-output externalization", sandbox_id)
        return None


def _budget_content(
    content: str,
    *,
    tool_name: str,
    tool_call_id: str,
    config: ToolOutputConfig,
) -> tuple[str, str] | None:
    """Apply budget to *content* and name the applied transform.

    Returns ``(replacement, transform_kind)`` — ``"externalized"`` or
    ``"truncated"`` — or ``None`` if no change was needed.
    """
    threshold = config.tool_overrides.get(tool_name, config.externalize_min_chars)
    if threshold <= 0 and config.fallback_max_chars <= 0:
        return None
    if len(content) <= threshold and len(content) <= config.fallback_max_chars:
        return None

    if config.fallback_max_chars > 0 and len(content) > config.fallback_max_chars:
        logger.warning(
            "Fallback-truncating %s output: %d chars → %d max",
            tool_name,
            len(content),
            config.fallback_max_chars,
        )
        return (
            _build_fallback(
                content,
                tool_name=tool_name,
                max_chars=config.fallback_max_chars,
                head_chars=config.fallback_head_chars,
                tail_chars=config.fallback_tail_chars,
            ),
            "truncated",
        )

    return None


# ---------------------------------------------------------------------------
# Result patchers
# ---------------------------------------------------------------------------


def _patch_tool_message(
    msg: ToolMessage,
    config: ToolOutputConfig,
) -> ToolMessage:
    """Apply budget to a single ToolMessage. Returns the original if unchanged."""
    tool_name = msg.name or "unknown"
    if tool_name in config.exempt_tools:
        return msg

    text = _message_text(msg.content)
    if text is None:
        return msg

    budgeted = _budget_content(
        text,
        tool_name=tool_name,
        tool_call_id=msg.tool_call_id or "",
        config=config,
    )
    if budgeted is None:
        return msg
    replacement, transform_kind = budgeted

    update: dict[str, Any] = {"content": replacement}
    if getattr(msg, "response_metadata", None):
        update["response_metadata"] = dict(msg.response_metadata)
    new_kwargs = dict(getattr(msg, "additional_kwargs", None) or {})
    append_tool_transform(new_kwargs, transform_kind, by="ToolOutputBudgetMiddleware")
    update["additional_kwargs"] = new_kwargs
    return msg.model_copy(update=update)


def _effective_trigger(tool_name: str, config: ToolOutputConfig) -> int:
    """Smallest content length that could trigger budgeting for *tool_name*.

    Mirrors the trigger conditions in :func:`_budget_content` (per-tool
    externalize threshold OR global fallback), so the pre-scan never produces
    a false negative. Returns ``-1`` when nothing could ever trigger.
    """
    candidates: list[int] = []
    externalize = config.tool_overrides.get(tool_name, config.externalize_min_chars)
    if externalize > 0:
        candidates.append(externalize)
    if config.fallback_max_chars > 0:
        candidates.append(config.fallback_max_chars)
    return min(candidates) if candidates else -1


def _tool_message_over_budget(msg: ToolMessage, config: ToolOutputConfig) -> bool:
    """Cheap, per-tool-aware check: is this ToolMessage non-exempt and over its trigger?"""
    if (msg.name or "") in config.exempt_tools:
        return False
    trigger = _effective_trigger(msg.name or "", config)
    if trigger < 0:
        return False
    text = _message_text(msg.content)
    return text is not None and len(text) > trigger


def _needs_budget(result: ToolMessage | Command, config: ToolOutputConfig) -> bool:
    """Fast check whether *result* could need budgeting (avoids thread offload for small outputs)."""
    if isinstance(result, ToolMessage):
        return _tool_message_over_budget(result, config)
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        for msg in update.get("messages", []):
            if isinstance(msg, ToolMessage) and _tool_message_over_budget(msg, config):
                return True
    return False


def _patch_result(
    result: ToolMessage | Command,
    config: ToolOutputConfig,
) -> ToolMessage | Command:
    """Apply budget to a tool call result (ToolMessage or Command)."""
    if isinstance(result, ToolMessage):
        return _patch_tool_message(result, config)

    update = getattr(result, "update", None)
    if not isinstance(update, dict):
        return result

    messages = update.get("messages")
    if not isinstance(messages, list):
        return result

    new_messages: list[Any] = []
    changed = False
    for msg in messages:
        if isinstance(msg, ToolMessage):
            patched = _patch_tool_message(msg, config)
            if patched is not msg:
                changed = True
            new_messages.append(patched)
        else:
            new_messages.append(msg)

    if not changed:
        return result

    return dc_replace(result, update={**update, "messages": new_messages})


def _output_storage_for_request(request: ToolCallRequest, app_config: Any | None) -> OutputsStorage | None:
    """Resolve the configured shared outputs store for this tool invocation."""
    runtime = getattr(request, "runtime", None)
    context = getattr(runtime, "context", None) if runtime is not None else None
    thread_id = context.get("thread_id") if isinstance(context, dict) else None
    if not thread_id or app_config is None or runtime is None:
        return None
    try:
        return OutputsStorage.from_app_config(app_config, user_id=resolve_runtime_user_id(runtime), thread_id=str(thread_id))
    except Exception:
        logger.exception("Shared output storage is unavailable for tool-output externalization")
        return None


async def _externalize_to_object_storage(
    content: str,
    *,
    tool_name: str,
    tool_call_id: str,
    storage_subdir: str,
    outputs: OutputsStorage,
    sandbox: Sandbox | None,
) -> str | None:
    if os.path.isabs(storage_subdir) or ".." in storage_subdir:
        return None
    filename = _build_externalized_filename(tool_name=tool_name, tool_call_id=tool_call_id)
    relative_path = f"{storage_subdir}/{filename}"
    try:
        # Object storage is committed before the sandbox projection.  A failed
        # projection does not create a local fallback; the next run rehydrates
        # the authoritative object and remains able to read it.
        await outputs.write_bytes(relative_path, content.encode("utf-8"), content_type="text/plain; charset=utf-8")
        if sandbox is not None:
            virtual_path = outputs.virtual_path(relative_path)
            await asyncio.to_thread(sandbox.execute_command, f"mkdir -p {shlex.quote(virtual_path.rsplit('/', 1)[0])}")
            await asyncio.to_thread(sandbox.write_file, virtual_path, content)
        return outputs.virtual_path(relative_path)
    except Exception:
        logger.exception("Failed to externalize %s output to shared object storage", tool_name)
        return None


async def _patch_tool_message_async(
    msg: ToolMessage,
    config: ToolOutputConfig,
    *,
    outputs: OutputsStorage | None,
    sandbox: Sandbox | None,
) -> ToolMessage:
    if (msg.name or "unknown") in config.exempt_tools:
        return msg
    text = _message_text(msg.content)
    if text is None:
        return msg
    tool_name = msg.name or "unknown"
    threshold = config.tool_overrides.get(tool_name, config.externalize_min_chars)
    replacement: str | None = None
    transform_kind: str | None = None
    if threshold > 0 and len(text) > threshold and outputs is not None:
        virtual_path = await _externalize_to_object_storage(
            text,
            tool_name=tool_name,
            tool_call_id=msg.tool_call_id or "",
            storage_subdir=config.storage_subdir,
            outputs=outputs,
            sandbox=sandbox,
        )
        if virtual_path is not None:
            replacement = _build_preview(text, tool_name=tool_name, virtual_path=virtual_path, head_chars=config.preview_head_chars, tail_chars=config.preview_tail_chars)
            transform_kind = "externalized"
    if replacement is None and config.fallback_max_chars > 0 and len(text) > config.fallback_max_chars:
        replacement = _build_fallback(text, tool_name=tool_name, max_chars=config.fallback_max_chars, head_chars=config.fallback_head_chars, tail_chars=config.fallback_tail_chars)
        transform_kind = "truncated"
    if replacement is None or transform_kind is None:
        return msg
    new_kwargs = dict(getattr(msg, "additional_kwargs", None) or {})
    append_tool_transform(new_kwargs, transform_kind, by="ToolOutputBudgetMiddleware")
    return msg.model_copy(update={"content": replacement, "additional_kwargs": new_kwargs, "response_metadata": dict(getattr(msg, "response_metadata", None) or {})})


async def _patch_result_async(result: ToolMessage | Command, config: ToolOutputConfig, *, outputs: OutputsStorage | None, sandbox: Sandbox | None) -> ToolMessage | Command:
    if isinstance(result, ToolMessage):
        return await _patch_tool_message_async(result, config, outputs=outputs, sandbox=sandbox)
    update = getattr(result, "update", None)
    if not isinstance(update, dict) or not isinstance(update.get("messages"), list):
        return result
    messages = [await _patch_tool_message_async(message, config, outputs=outputs, sandbox=sandbox) if isinstance(message, ToolMessage) else message for message in update["messages"]]
    if all(before is after for before, after in zip(update["messages"], messages, strict=True)):
        return result
    return dc_replace(result, update={**update, "messages": messages})


def _patch_model_messages(messages: list[Any], config: ToolOutputConfig) -> list[Any] | None:
    """Apply budget to historical ToolMessages in a model request. Returns ``None`` if unchanged.

    A cheap pre-scan bails out before allocating a new list when no historical
    ToolMessage exceeds the budget — the common case once every result has
    already been budgeted at tool-call time, so a long history is not rebuilt
    on every model call.

    Historical messages do not get a ``sandbox`` argument: any oversized tool
    message in history was already budgeted (and possibly externalized) at
    tool-call time, so the only thing left for the history path to do is
    inline fallback truncation, which needs no sandbox.
    """
    if not any(isinstance(msg, ToolMessage) and _tool_message_over_budget(msg, config) for msg in messages):
        return None

    updated: list[Any] = []
    changed = False
    for msg in messages:
        if isinstance(msg, ToolMessage):
            patched = _patch_tool_message(msg, config)
            if patched is not msg:
                changed = True
            updated.append(patched)
        else:
            updated.append(msg)
    return updated if changed else None


# ---------------------------------------------------------------------------
# Superseded write payload elision (issue #5328, step 2)
# ---------------------------------------------------------------------------

_WRITE_TOOL = "write_file"
# A successful call of these tools changes the file, so every earlier write's
# content is stale afterwards. ``str_replace`` payloads are never elided
# themselves: they are usually small, and the issue scopes step 2 to
# ``write_file.content``.
_FILE_MODIFYING_TOOLS = frozenset({"write_file", "str_replace"})
# A non-error read (full, ranged, or head-truncated — ``partial_success``)
# showed the model the on-disk file, which is what the placeholder points at.
_FILE_READING_TOOLS = frozenset({"read_file"})
_SUPERSEDING_READ_STATUSES = frozenset({"success", "partial_success"})
# Deterministic for a given payload so repeated model calls keep the same
# request prefix (prompt caching) instead of drifting. Framework-owned static
# text plus a character count; no model-supplied value is interpolated (the
# path stays visible in the call's own ``path`` argument).
_ELIDED_WRITE_CONTENT_TEMPLATE = "[content elided: {chars} chars; this write_file call succeeded and the file was read or modified again afterwards, so the on-disk file is the current version; call read_file on its path to see it]"


def elide_superseded_write_payloads(messages: list[Any], *, min_chars: int, keep_recent: int) -> list[Any] | None:
    """Return ``messages`` with superseded ``write_file`` content replaced by placeholders, or ``None`` if unchanged.

    Only the policy lives here. A call qualifies when its paired result is
    stamped ``deerflow_tool_meta.status == "success"``, its ``content`` is a
    string of at least ``min_chars`` characters, a *later* message holds a
    successful ``read_file`` / ``write_file`` / ``str_replace`` of the same
    normalized path, and it is not among the ``keep_recent`` newest successful
    writes. Calls are paired with results per occurrence
    (``tool_call_args.pair_tool_call_results``), and "later" means a later
    message index: the calls of one AIMessage ran concurrently, so a same-turn
    read may predate the write and never supersedes it. The surface-by-surface
    rewrite is ``rewrite_messages_tool_call_args``, which never mutates the
    input and passes untouched messages through by identity, so the stored
    history keeps the original arguments and the output is identical across
    model calls. The policy is monotonic: once a write is elided, more history
    never brings its content back.
    """
    if not _has_elidable_write(messages, min_chars):
        return None

    latest_touch: dict[str, int] = {}
    successful_writes: list[tuple[ToolCallOccurrence, str]] = []
    for occurrence in pair_tool_call_results(messages):
        path = _normalized_path_arg(occurrence.args)
        if path is None:
            continue
        name = occurrence.name
        if name in _FILE_MODIFYING_TOOLS:
            if _result_status(occurrence.result) != "success":
                continue
            if name == _WRITE_TOOL:
                successful_writes.append((occurrence, path))
        elif name in _FILE_READING_TOOLS:
            if _result_status(occurrence.result) not in _SUPERSEDING_READ_STATUSES:
                continue
        else:
            continue
        latest_touch[path] = max(latest_touch.get(path, -1), occurrence.index)

    replacements: dict[tuple[int, str], dict[str, Any]] = {}
    cutoff = max(0, len(successful_writes) - keep_recent)
    for occurrence, path in successful_writes[:cutoff]:
        content = occurrence.args.get("content")
        if not isinstance(content, str) or not content or len(content) < min_chars:
            continue
        if latest_touch.get(path, -1) <= occurrence.index:
            continue
        replacements[(id(occurrence.message), occurrence.call_id)] = {**occurrence.args, "content": _ELIDED_WRITE_CONTENT_TEMPLATE.format(chars=len(content))}
    if not replacements:
        return None

    def replacement_for(message: AIMessage, tool_call: dict[str, Any]) -> dict[str, Any] | None:
        return replacements.get((id(message), tool_call["id"]))

    return rewrite_messages_tool_call_args(messages, replacement_for)


def _has_elidable_write(messages: list[Any], min_chars: int) -> bool:
    """Cheap pre-scan so a history without a sizeable ``write_file`` call is never paired or rebuilt."""
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for tool_call in message.tool_calls or ():
            if not isinstance(tool_call, dict) or tool_call.get("name") != _WRITE_TOOL:
                continue
            args = tool_call.get("args")
            content = args.get("content") if isinstance(args, dict) else None
            if isinstance(content, str) and content and len(content) >= min_chars:
                return True
    return False


def _result_status(result: ToolMessage | None) -> str | None:
    """``deerflow_tool_meta.status`` of a paired result; ``None`` when unanswered or unstamped (never treated as success)."""
    if result is None:
        return None
    meta = (result.additional_kwargs or {}).get(TOOL_META_KEY)
    status = meta.get("status") if isinstance(meta, dict) else None
    return status if isinstance(status, str) else None


def _normalized_path_arg(args: Mapping[str, Any]) -> str | None:
    """The call's ``path`` argument normalized the way the read-before-write gate keys its marks."""
    path = args.get("path")
    return posixpath.normpath(path) if isinstance(path, str) and path else None


# ---------------------------------------------------------------------------
# Middleware class
# ---------------------------------------------------------------------------


class ToolOutputBudgetMiddleware(AgentMiddleware[AgentState]):
    """Enforce per-result budget on tool outputs via externalization or truncation."""

    def __init__(self, config: ToolOutputConfig | None = None, *, app_config: Any | None = None) -> None:
        super().__init__()
        self._config = config if config is not None else _default_config()
        self._app_config = app_config

    def release_policy_parameters(self) -> dict[str, object]:
        return {"config": self._config.model_dump(mode="python")}

    @classmethod
    def from_app_config(cls, app_config: Any) -> ToolOutputBudgetMiddleware:
        tool_output = getattr(app_config, "tool_output", None)
        if isinstance(tool_output, ToolOutputConfig):
            return cls(config=tool_output, app_config=app_config)
        return cls(app_config=app_config)

    # -- tool call hooks ---------------------------------------------------

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        result = handler(request)
        if not self._config.enabled:
            return result
        if not _needs_budget(result, self._config):
            return result
        # Synchronous middleware hooks have no safe way to await the async S3
        # port.  They deliberately truncate rather than writing persistent
        # local output; normal Gateway execution uses ``awrap_tool_call``.
        return _patch_result(result, self._config)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        result = await handler(request)
        if not self._config.enabled:
            return result
        if not _needs_budget(result, self._config):
            return result
        outputs = _output_storage_for_request(request, self._app_config)
        sandbox = _resolve_sandbox(request)
        return await _patch_result_async(result, self._config, outputs=outputs, sandbox=sandbox)

    # -- model call hooks (historical context budgeting) -------------------

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._budget_model_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        # Pure in-memory rewrite: no sandbox or file I/O, so it stays on the loop.
        return await handler(self._budget_model_request(request))

    def _budget_model_request(self, request: ModelRequest) -> ModelRequest:
        """Truncate oversized historical tool output and elide superseded write payloads in the request copy only."""
        if not self._config.enabled:
            return request
        original = getattr(request, "messages", None)
        if not isinstance(original, list):
            return request
        messages = original
        patched = _patch_model_messages(messages, self._config)
        if patched is not None:
            messages = patched
        if self._config.elide_superseded_writes:
            elided = elide_superseded_write_payloads(
                messages,
                min_chars=self._config.superseded_write_min_chars,
                keep_recent=self._config.keep_recent_writes,
            )
            if elided is not None:
                messages = elided
        if messages is original:
            return request
        return request.override(messages=messages)
