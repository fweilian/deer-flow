"""Run lifecycle service layer.

Centralizes the business logic for creating runs, formatting SSE
frames, and consuming stream bridge events.  Router modules
(``thread_runs``, ``runs``) are thin HTTP handlers that delegate here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, Request
from langchain_core.messages import HumanMessage

from app.gateway.deps import get_run_context, get_run_manager, get_run_store, get_stream_bridge
from app.gateway.utils import sanitize_log_param
from deerflow.runtime import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    ConflictError,
    DisconnectMode,
    RunManager,
    RunRecord,
    RunStatus,
    StreamBridge,
    UnsupportedStrategyError,
    run_agent,
)
from deerflow.runtime.runs.store.base import RunStore
from deerflow.runtime.user_context import AUTO

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSE formatting
# ---------------------------------------------------------------------------


def format_sse(event: str, data: Any, *, event_id: str | None = None) -> str:
    """Format a single SSE frame.

    Field order: ``event:`` -> ``data:`` -> ``id:`` (optional) -> blank line.
    This matches the LangGraph Platform wire format consumed by the
    ``useStream`` React hook and the Python ``langgraph-sdk`` SSE decoder.
    """
    payload = json.dumps(data, default=str, ensure_ascii=False)
    parts = [f"event: {event}", f"data: {payload}"]
    if event_id:
        parts.append(f"id: {event_id}")
    parts.append("")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Input / config helpers
# ---------------------------------------------------------------------------


def normalize_stream_modes(raw: list[str] | str | None) -> list[str]:
    """Normalize the stream_mode parameter to a list.

    Default matches what ``useStream`` expects: values + messages-tuple.
    """
    if raw is None:
        return ["values"]
    if isinstance(raw, str):
        return [raw]
    return raw if raw else ["values"]


def normalize_input(raw_input: dict[str, Any] | None) -> dict[str, Any]:
    """Convert LangGraph Platform input format to LangChain state dict."""
    if raw_input is None:
        return {}
    messages = raw_input.get("messages")
    if messages and isinstance(messages, list):
        converted = []
        for msg in messages:
            if isinstance(msg, dict):
                role = msg.get("role", msg.get("type", "user"))
                content = msg.get("content", "")
                if role in ("user", "human"):
                    converted.append(HumanMessage(content=content))
                else:
                    # TODO: handle other message types (system, ai, tool)
                    converted.append(HumanMessage(content=content))
            else:
                converted.append(msg)
        return {**raw_input, "messages": converted}
    description = raw_input.get("description")
    if isinstance(description, str) and description.strip():
        task_type = raw_input.get("task_type")
        content = description.strip()
        if isinstance(task_type, str) and task_type.strip():
            content = f"{content}\n\ntask_type: {task_type.strip()}"
        return {
            **raw_input,
            "messages": [HumanMessage(content=content)],
        }
    return raw_input


_DEFAULT_ASSISTANT_ID = "lead_agent"
_DEFAULT_CRON_MULTITASK_STRATEGY = "reject"
_CRON_LLM_DEBUG_ENV = "DEERFLOW_CRON_LLM_DEBUG"


# Whitelist of run-context keys that the langgraph-compat layer forwards from
# ``body.context`` into the run config. ``config["context"]`` exists in
# LangGraph >=0.6, but these values must be written to both ``configurable``
# (for legacy ``_get_runtime_config`` consumers) and ``context`` because
# LangGraph >=1.1.9 no longer makes ``ToolRuntime.context`` fall back to
# ``configurable`` for consumers like ``setup_agent``.
_CONTEXT_CONFIGURABLE_KEYS: frozenset[str] = frozenset(
    {
        "model_name",
        "mode",
        "thinking_enabled",
        "reasoning_effort",
        "is_plan_mode",
        "subagent_enabled",
        "max_concurrent_subagents",
        "agent_name",
        "is_bootstrap",
    }
)


def merge_run_context_overrides(config: dict[str, Any], context: Mapping[str, Any] | None) -> None:
    """Merge whitelisted keys from ``body.context`` into both ``config['configurable']``
    and ``config['context']`` so they are visible to legacy configurable readers and
    to LangGraph ``ToolRuntime.context`` consumers (e.g. the ``setup_agent`` tool —
    see issue #2677)."""
    if not context:
        return
    configurable = config.setdefault("configurable", {})
    runtime_context = config.setdefault("context", {})
    for key in _CONTEXT_CONFIGURABLE_KEYS:
        if key in context:
            if isinstance(configurable, dict):
                configurable.setdefault(key, context[key])
            if isinstance(runtime_context, dict):
                runtime_context.setdefault(key, context[key])


def normalize_cron_multitask_strategy(strategy: str | None) -> str:
    """Map cron concurrency defaults to currently supported run-manager values.

    Existing cron jobs may still carry ``enqueue`` from earlier scheduler
    defaults, but RunManager currently supports only ``reject``,
    ``interrupt``, and ``rollback``. Degrade legacy ``enqueue`` jobs to
    ``reject`` so scheduled runs still execute instead of failing every
    dispatch attempt with HTTP 501.
    """

    normalized = (strategy or "").strip() or _DEFAULT_CRON_MULTITASK_STRATEGY
    if normalized == "enqueue":
        logger.warning(
            "Cron multitask_strategy 'enqueue' is not supported by RunManager; degrading to '%s'.",
            _DEFAULT_CRON_MULTITASK_STRATEGY,
        )
        return _DEFAULT_CRON_MULTITASK_STRATEGY
    return normalized


def _cron_llm_debug_enabled() -> bool:
    value = os.getenv(_CRON_LLM_DEBUG_ENV, "")
    return value.strip().lower() in {"1", "true", "yes", "on", "debug"}


def _safe_debug_json(value: Any, *, max_chars: int = 20000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = repr(value)
    if len(text) > max_chars:
        return f"{text[:max_chars]}...<truncated {len(text) - max_chars} chars>"
    return text


def log_cron_llm_debug(
    *,
    thread_id: str,
    execution_thread_id: str,
    assistant_id: str | None,
    input_payload: Any,
    metadata: dict[str, Any] | None,
    config: dict[str, Any] | None,
    context: Mapping[str, Any] | None,
) -> None:
    if not _cron_llm_debug_enabled():
        return
    logger.debug(
        "Cron LLM debug input thread_id=%s execution_thread_id=%s assistant_id=%s payload=%s",
        sanitize_log_param(thread_id),
        sanitize_log_param(execution_thread_id),
        sanitize_log_param(assistant_id or ""),
        _safe_debug_json(
            {
                "input": input_payload,
                "metadata": metadata,
                "config": config,
                "context": dict(context or {}),
            }
        ),
    )


def resolve_run_owner_id(metadata: Mapping[str, Any] | None) -> str | None | object:
    """Resolve the owner id for a run that may execute after request scope ends.

    Cron runs execute from a background scheduler without request-auth context, so
    they must carry an explicit owner id. Normal request-driven runs can keep using
    ``AUTO`` and rely on the request middleware to set the contextvar.
    """

    scheduler_meta = (metadata or {}).get("scheduler") or {}
    job_meta = scheduler_meta.get("job") or {}
    creator_user_id = job_meta.get("creator_user_id")
    if isinstance(creator_user_id, str):
        normalized = creator_user_id.strip()
        if normalized:
            return normalized
    return AUTO


def resolve_agent_factory(assistant_id: str | None):
    """Resolve the agent factory callable from config.

    Custom agents are implemented as ``lead_agent`` + an ``agent_name``
    injected into ``configurable`` or ``context`` — see
    :func:`build_run_config`.  All ``assistant_id`` values therefore map to the
    same factory; the routing happens inside ``make_lead_agent`` when it reads
    ``cfg["agent_name"]``.
    """
    from deerflow.agents.lead_agent.agent import make_lead_agent

    return make_lead_agent


def build_run_config(
    thread_id: str,
    request_config: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    *,
    assistant_id: str | None = None,
) -> dict[str, Any]:
    """Build a RunnableConfig dict for the agent.

    When *assistant_id* refers to a custom agent (anything other than
    ``"lead_agent"`` / ``None``), the name is forwarded as ``agent_name`` in
    whichever runtime options container is active: ``context`` for
    LangGraph >= 0.6.0 requests, otherwise ``configurable``.
    ``make_lead_agent`` reads this key to load the matching
    ``agents/<name>/SOUL.md`` and per-agent config — without it the agent
    silently runs as the default lead agent.

    This mirrors the channel manager's ``_resolve_run_params`` logic so that
    the LangGraph Platform-compatible HTTP API and the IM channel path behave
    identically.
    """
    config: dict[str, Any] = {"recursion_limit": 100}
    if request_config:
        # LangGraph >= 0.6.0 introduced ``context`` as the preferred way to
        # pass thread-level data and rejects requests that include both
        # ``configurable`` and ``context``.  If the caller already sends
        # ``context``, honour it and skip our own ``configurable`` dict.
        if "context" in request_config:
            if "configurable" in request_config:
                logger.warning(
                    "build_run_config: client sent both 'context' and 'configurable'; preferring 'context' (LangGraph >= 0.6.0). thread_id=%s, caller_configurable keys=%s",
                    thread_id,
                    list(request_config.get("configurable", {}).keys()),
                )
            context_value = request_config["context"]
            if context_value is None:
                context = {}
            elif isinstance(context_value, Mapping):
                context = dict(context_value)
            else:
                raise ValueError("request config 'context' must be a mapping or null.")
            config["context"] = context
        else:
            configurable = {"thread_id": thread_id}
            configurable.update(request_config.get("configurable", {}))
            config["configurable"] = configurable
        for k, v in request_config.items():
            if k not in ("configurable", "context"):
                config[k] = v
    else:
        config["configurable"] = {"thread_id": thread_id}

    # Inject custom agent name when the caller specified a non-default assistant.
    # Honour an explicit agent_name in the active runtime options container.
    if assistant_id and assistant_id != _DEFAULT_ASSISTANT_ID:
        normalized = assistant_id.strip().lower().replace("_", "-")
        if not normalized or not re.fullmatch(r"[a-z0-9-]+", normalized):
            raise ValueError(f"Invalid assistant_id {assistant_id!r}: must contain only letters, digits, and hyphens after normalization.")
        if "configurable" in config:
            target = config["configurable"]
        elif "context" in config:
            target = config["context"]
        else:
            target = config.setdefault("configurable", {})
        if target is not None and "agent_name" not in target:
            target["agent_name"] = normalized
    if metadata:
        config.setdefault("metadata", {}).update(metadata)
    return config


def _build_cron_scheduler_metadata(job: Any, fire: Any, *, idempotency_key: str) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "fire_id": fire.fire_id,
        "scheduled_fire_at": fire.scheduled_fire_at,
        "idempotency_key": idempotency_key,
        "job": {
            "job_id": job.job_id,
            "thread_id": job.thread_id,
            "execution_thread_id": getattr(job, "execution_thread_id", job.thread_id),
            "assistant_id": job.assistant_id,
            "cron_expr": job.cron,
            "timezone": job.timezone,
            "creator_user_id": getattr(job, "creator_user_id", "default"),
            "input": job.input,
        },
        "delivery": job.delivery.model_dump() if getattr(job, "delivery", None) is not None else None,
        "fire": {
            "fire_id": fire.fire_id,
            "job_id": fire.job_id,
            "scheduled_fire_at": fire.scheduled_fire_at,
        },
    }


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------


async def start_run(
    body: Any,
    thread_id: str,
    request: Request,
) -> RunRecord:
    """Create a RunRecord and launch the background agent task.

    Parameters
    ----------
    body : RunCreateRequest
        The validated request body (typed as Any to avoid circular import
        with the router module that defines the Pydantic model).
    thread_id : str
        Target thread.
    request : Request
        FastAPI request — used to retrieve singletons from ``app.state``.
    """
    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    run_ctx = get_run_context(request)

    return await _launch_run(body, thread_id, bridge=bridge, run_mgr=run_mgr, run_ctx=run_ctx)


async def find_existing_cron_run(run_store: RunStore, thread_id: str, idempotency_key: str) -> dict[str, Any] | None:
    return await run_store.find_run_by_scheduler_idempotency_key(thread_id, idempotency_key)


async def start_cron_run(job: Any, fire: Any, request: Request) -> RunRecord | SimpleNamespace:
    run_store = get_run_store(request)
    execution_thread_id = getattr(job, "execution_thread_id", None) or job.thread_id
    idempotency_key = f"cron:{job.job_id}:{int(fire.scheduled_fire_at)}"
    existing = await find_existing_cron_run(run_store, execution_thread_id, idempotency_key)
    if existing is not None:
        return SimpleNamespace(run_id=existing["run_id"])

    bridge = get_stream_bridge(request)
    run_mgr = get_run_manager(request)
    run_ctx = get_run_context(request)

    return await start_cron_run_with_deps(
        job,
        fire,
        thread_id=execution_thread_id,
        bridge=bridge,
        run_mgr=run_mgr,
        run_ctx=run_ctx,
    )


async def start_cron_run_with_deps(
    job: Any,
    fire: Any,
    *,
    thread_id: str,
    bridge: StreamBridge,
    run_mgr: RunManager,
    run_ctx: Any,
) -> RunRecord:
    idempotency_key = f"cron:{job.job_id}:{int(fire.scheduled_fire_at)}"
    metadata = dict(job.metadata or {})
    metadata["scheduler"] = _build_cron_scheduler_metadata(job, fire, idempotency_key=idempotency_key)
    multitask_strategy = normalize_cron_multitask_strategy(getattr(job, "multitask_strategy", None))
    cron_request = SimpleNamespace(
        assistant_id=job.assistant_id,
        input=job.input,
        metadata=metadata,
        config=job.config,
        context=job.context,
        on_disconnect="continue",
        multitask_strategy=multitask_strategy,
        stream_mode=None,
        stream_subgraphs=False,
        interrupt_before=None,
        interrupt_after=None,
    )
    log_cron_llm_debug(
        thread_id=job.thread_id,
        execution_thread_id=thread_id,
        assistant_id=job.assistant_id,
        input_payload=job.input,
        metadata=metadata,
        config=job.config,
        context=job.context,
    )
    return await _launch_run(cron_request, thread_id, bridge=bridge, run_mgr=run_mgr, run_ctx=run_ctx)


async def _launch_run(
    body: Any,
    thread_id: str,
    *,
    bridge: StreamBridge,
    run_mgr: RunManager,
    run_ctx: Any,
) -> RunRecord:
    disconnect = DisconnectMode.cancel if body.on_disconnect == "cancel" else DisconnectMode.continue_
    owner_user_id = resolve_run_owner_id(body.metadata or {})

    try:
        record = await run_mgr.create_or_reject(
            thread_id,
            body.assistant_id,
            on_disconnect=disconnect,
            metadata=body.metadata or {},
            kwargs={"input": body.input, "config": body.config},
            user_id=owner_user_id,
            multitask_strategy=body.multitask_strategy,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnsupportedStrategyError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    # Upsert thread metadata so the thread appears in /threads/search,
    # even for threads that were never explicitly created via POST /threads
    # (e.g. stateless runs).
    try:
        existing = await run_ctx.thread_store.get(thread_id, user_id=owner_user_id)
        if existing is None:
            await run_ctx.thread_store.create(
                thread_id,
                assistant_id=body.assistant_id,
                user_id=owner_user_id,
                metadata=body.metadata,
            )
        else:
            await run_ctx.thread_store.update_status(thread_id, "running", user_id=owner_user_id)
    except Exception:
        logger.warning("Failed to upsert thread_meta for %s (non-fatal)", sanitize_log_param(thread_id))

    agent_factory = resolve_agent_factory(body.assistant_id)
    graph_input = normalize_input(body.input)
    config = build_run_config(thread_id, body.config, body.metadata, assistant_id=body.assistant_id)

    # Merge DeerFlow-specific context overrides into both ``configurable`` and ``context``.
    # The ``context`` field is a custom extension for the langgraph-compat layer
    # that carries agent configuration (model_name, thinking_enabled, etc.).
    # Only agent-relevant keys are forwarded; unknown keys (e.g. thread_id) are ignored.
    merge_run_context_overrides(config, getattr(body, "context", None))

    stream_modes = normalize_stream_modes(body.stream_mode)

    task = asyncio.create_task(
        run_agent(
            bridge,
            run_mgr,
            record,
            ctx=run_ctx,
            agent_factory=agent_factory,
            graph_input=graph_input,
            config=config,
            stream_modes=stream_modes,
            stream_subgraphs=body.stream_subgraphs,
            interrupt_before=body.interrupt_before,
            interrupt_after=body.interrupt_after,
        )
    )
    record.task = task

    # Title sync is handled by worker.py's finally block which reads the
    # title from the checkpoint and calls thread_store.update_display_name
    # after the run completes.

    return record


async def sse_consumer(
    bridge: StreamBridge,
    record: RunRecord,
    request: Request,
    run_mgr: RunManager,
):
    """Async generator that yields SSE frames from the bridge.

    The ``finally`` block implements ``on_disconnect`` semantics:
    - ``cancel``: abort the background task on client disconnect.
    - ``continue``: let the task run; events are discarded.
    """
    last_event_id = request.headers.get("Last-Event-ID")
    try:
        async for entry in bridge.subscribe(record.run_id, last_event_id=last_event_id):
            if await request.is_disconnected():
                break

            if entry is HEARTBEAT_SENTINEL:
                yield ": heartbeat\n\n"
                continue

            if entry is END_SENTINEL:
                yield format_sse("end", None, event_id=entry.id or None)
                return

            yield format_sse(entry.event, entry.data, event_id=entry.id or None)

    finally:
        if record.status in (RunStatus.pending, RunStatus.running):
            if record.on_disconnect == DisconnectMode.cancel:
                await run_mgr.cancel(record.run_id)
