"""Tests for app.gateway.services — run lifecycle service layer."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.requests import Request


def test_format_sse_basic():
    from app.gateway.services import format_sse

    frame = format_sse("metadata", {"run_id": "abc"})
    assert frame.startswith("event: metadata\n")
    assert "data: " in frame
    parsed = json.loads(frame.split("data: ")[1].split("\n")[0])
    assert parsed["run_id"] == "abc"


def test_format_sse_with_event_id():
    from app.gateway.services import format_sse

    frame = format_sse("metadata", {"run_id": "abc"}, event_id="123-0")
    assert "id: 123-0" in frame


def test_format_sse_end_event_null():
    from app.gateway.services import format_sse

    frame = format_sse("end", None)
    assert "data: null" in frame


def test_format_sse_no_event_id():
    from app.gateway.services import format_sse

    frame = format_sse("values", {"x": 1})
    assert "id:" not in frame


def test_normalize_stream_modes_none():
    from app.gateway.services import normalize_stream_modes

    assert normalize_stream_modes(None) == ["values"]


def test_normalize_stream_modes_string():
    from app.gateway.services import normalize_stream_modes

    assert normalize_stream_modes("messages-tuple") == ["messages-tuple"]


def test_normalize_stream_modes_list():
    from app.gateway.services import normalize_stream_modes

    assert normalize_stream_modes(["values", "messages-tuple"]) == ["values", "messages-tuple"]


def test_normalize_stream_modes_empty_list():
    from app.gateway.services import normalize_stream_modes

    assert normalize_stream_modes([]) == ["values"]


def test_normalize_input_none():
    from app.gateway.services import normalize_input

    assert normalize_input(None) == {}


def test_normalize_input_with_messages():
    from app.gateway.services import normalize_input

    result = normalize_input({"messages": [{"role": "user", "content": "hi"}]})
    assert len(result["messages"]) == 1
    assert result["messages"][0].content == "hi"


def test_normalize_input_passthrough():
    from app.gateway.services import normalize_input

    result = normalize_input({"custom_key": "value"})
    assert result == {"custom_key": "value"}


def test_normalize_input_legacy_cron_description_shape():
    from app.gateway.services import normalize_input

    result = normalize_input({"description": "分析昨日 xxxx 数据并输出报告", "task_type": "xxx-performance"})

    assert len(result["messages"]) == 1
    assert result["messages"][0].content == "分析昨日 xxxx 数据并输出报告\n\ntask_type: xxx-performance"
    assert result["description"] == "分析昨日 xxxx 数据并输出报告"
    assert result["task_type"] == "xxx-performance"


def test_build_run_config_basic():
    from app.gateway.services import build_run_config

    config = build_run_config("thread-1", None, None)
    assert config["configurable"]["thread_id"] == "thread-1"
    assert config["recursion_limit"] == 100


def test_build_run_config_with_overrides():
    from app.gateway.services import build_run_config

    config = build_run_config(
        "thread-1",
        {"configurable": {"model_name": "gpt-4"}, "tags": ["test"]},
        {"user": "alice"},
    )
    assert config["configurable"]["model_name"] == "gpt-4"
    assert config["tags"] == ["test"]
    assert config["metadata"]["user"] == "alice"


# ---------------------------------------------------------------------------
# Regression tests for issue #1644:
# assistant_id not mapped to agent_name → custom agent SOUL.md never loaded
# ---------------------------------------------------------------------------


def test_build_run_config_custom_agent_injects_agent_name():
    """Custom assistant_id must be forwarded as configurable['agent_name']."""
    from app.gateway.services import build_run_config

    config = build_run_config("thread-1", None, None, assistant_id="finalis")
    assert config["configurable"]["agent_name"] == "finalis"


def test_build_run_config_lead_agent_no_agent_name():
    """'lead_agent' assistant_id must NOT inject configurable['agent_name']."""
    from app.gateway.services import build_run_config

    config = build_run_config("thread-1", None, None, assistant_id="lead_agent")
    assert "agent_name" not in config["configurable"]


def test_build_run_config_none_assistant_id_no_agent_name():
    """None assistant_id must NOT inject configurable['agent_name']."""
    from app.gateway.services import build_run_config

    config = build_run_config("thread-1", None, None, assistant_id=None)
    assert "agent_name" not in config["configurable"]


def test_build_run_config_explicit_agent_name_not_overwritten():
    """An explicit configurable['agent_name'] in the request must take precedence."""
    from app.gateway.services import build_run_config

    config = build_run_config(
        "thread-1",
        {"configurable": {"agent_name": "explicit-agent"}},
        None,
        assistant_id="other-agent",
    )
    assert config["configurable"]["agent_name"] == "explicit-agent"


def test_build_run_config_context_custom_agent_injects_agent_name():
    """Custom assistant_id must be forwarded as context['agent_name'] in context mode."""
    from app.gateway.services import build_run_config

    config = build_run_config(
        "thread-1",
        {"context": {"model_name": "deepseek-v3"}},
        None,
        assistant_id="finalis",
    )

    assert config["context"]["agent_name"] == "finalis"
    assert "configurable" not in config


def test_resolve_agent_factory_returns_make_lead_agent():
    """resolve_agent_factory always returns make_lead_agent regardless of assistant_id."""
    from app.gateway.services import resolve_agent_factory
    from deerflow.agents.lead_agent.agent import make_lead_agent

    assert resolve_agent_factory(None) is make_lead_agent
    assert resolve_agent_factory("lead_agent") is make_lead_agent
    assert resolve_agent_factory("finalis") is make_lead_agent
    assert resolve_agent_factory("custom-agent-123") is make_lead_agent


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Regression tests for issue #1699:
# context field in langgraph-compat requests not merged into configurable
# ---------------------------------------------------------------------------


def test_run_create_request_accepts_context():
    """RunCreateRequest must accept the ``context`` field without dropping it."""
    from app.gateway.routers.thread_runs import RunCreateRequest

    body = RunCreateRequest(
        input={"messages": [{"role": "user", "content": "hi"}]},
        context={
            "model_name": "deepseek-v3",
            "thinking_enabled": True,
            "is_plan_mode": True,
            "subagent_enabled": True,
            "thread_id": "some-thread-id",
        },
    )
    assert body.context is not None
    assert body.context["model_name"] == "deepseek-v3"
    assert body.context["is_plan_mode"] is True
    assert body.context["subagent_enabled"] is True


def test_run_create_request_context_defaults_to_none():
    """RunCreateRequest without context should default to None (backward compat)."""
    from app.gateway.routers.thread_runs import RunCreateRequest

    body = RunCreateRequest(input=None)
    assert body.context is None


def test_context_merges_into_configurable():
    """Context values must be merged into config['configurable'] by start_run.

    Since start_run is async and requires many dependencies, we test the
    merging logic directly by simulating what start_run does.
    """
    from app.gateway.services import build_run_config

    # Simulate the context merging logic from start_run
    config = build_run_config("thread-1", None, None)

    context = {
        "model_name": "deepseek-v3",
        "mode": "ultra",
        "reasoning_effort": "high",
        "thinking_enabled": True,
        "is_plan_mode": True,
        "subagent_enabled": True,
        "max_concurrent_subagents": 5,
        "thread_id": "should-be-ignored",
    }

    _CONTEXT_CONFIGURABLE_KEYS = {
        "model_name",
        "mode",
        "thinking_enabled",
        "reasoning_effort",
        "is_plan_mode",
        "subagent_enabled",
        "max_concurrent_subagents",
    }
    configurable = config.setdefault("configurable", {})
    for key in _CONTEXT_CONFIGURABLE_KEYS:
        if key in context:
            configurable.setdefault(key, context[key])

    assert config["configurable"]["model_name"] == "deepseek-v3"
    assert config["configurable"]["thinking_enabled"] is True
    assert config["configurable"]["is_plan_mode"] is True
    assert config["configurable"]["subagent_enabled"] is True
    assert config["configurable"]["max_concurrent_subagents"] == 5
    assert config["configurable"]["reasoning_effort"] == "high"
    assert config["configurable"]["mode"] == "ultra"
    # thread_id from context should NOT override the one from build_run_config
    assert config["configurable"]["thread_id"] == "thread-1"
    # Non-allowlisted keys should not appear
    assert "thread_id" not in {k for k in context if k in _CONTEXT_CONFIGURABLE_KEYS}


def test_merge_run_context_overrides_propagates_to_runtime_context():
    """Regression for issue #2677: ``agent_name`` (and other whitelisted keys) from
    ``body.context`` must be propagated into BOTH ``config['configurable']`` and
    ``config['context']``. Previously only ``configurable`` was populated, so after
    the LangGraph 1.1.x upgrade removed the fallback from ``configurable``, the
    ``setup_agent`` tool read ``runtime.context`` with ``agent_name=None`` and
    silently wrote SOUL.md to the global base_dir.
    """
    from app.gateway.services import build_run_config, merge_run_context_overrides

    config = build_run_config("thread-1", None, None)
    merge_run_context_overrides(config, {"agent_name": "my-agent", "is_bootstrap": True, "thread_id": "ignored"})

    assert config["configurable"]["agent_name"] == "my-agent"
    assert config["configurable"]["is_bootstrap"] is True
    assert config["context"]["agent_name"] == "my-agent"
    assert config["context"]["is_bootstrap"] is True
    # Non-whitelisted keys are not forwarded.
    assert "thread_id" not in config["context"]


def test_merge_run_context_overrides_noop_for_empty_context():
    from app.gateway.services import build_run_config, merge_run_context_overrides

    config = build_run_config("thread-1", None, None)
    before = {k: dict(v) if isinstance(v, dict) else v for k, v in config.items()}
    merge_run_context_overrides(config, None)
    merge_run_context_overrides(config, {})
    assert config == before


def test_context_does_not_override_existing_configurable():
    """Values already in config.configurable must NOT be overridden by context."""
    from app.gateway.services import build_run_config

    config = build_run_config(
        "thread-1",
        {"configurable": {"model_name": "gpt-4", "is_plan_mode": False}},
        None,
    )

    context = {
        "model_name": "deepseek-v3",
        "is_plan_mode": True,
        "subagent_enabled": True,
    }

    _CONTEXT_CONFIGURABLE_KEYS = {
        "model_name",
        "mode",
        "thinking_enabled",
        "reasoning_effort",
        "is_plan_mode",
        "subagent_enabled",
        "max_concurrent_subagents",
    }
    configurable = config.setdefault("configurable", {})
    for key in _CONTEXT_CONFIGURABLE_KEYS:
        if key in context:
            configurable.setdefault(key, context[key])

    # Existing values must NOT be overridden
    assert config["configurable"]["model_name"] == "gpt-4"
    assert config["configurable"]["is_plan_mode"] is False
    # New values should be added
    assert config["configurable"]["subagent_enabled"] is True


# ---------------------------------------------------------------------------
# build_run_config — context / configurable precedence (LangGraph >= 0.6.0)
# ---------------------------------------------------------------------------


def test_build_run_config_with_context():
    """When caller sends 'context', prefer it over 'configurable'."""
    from app.gateway.services import build_run_config

    config = build_run_config(
        "thread-1",
        {"context": {"user_id": "u-42", "thread_id": "thread-1"}},
        None,
    )
    assert "context" in config
    assert config["context"]["user_id"] == "u-42"
    assert "configurable" not in config
    assert config["recursion_limit"] == 100


def test_build_run_config_null_context_becomes_empty_context():
    """When caller sends context=null, treat it as an empty context object."""
    from app.gateway.services import build_run_config

    config = build_run_config("thread-1", {"context": None}, None)

    assert config["context"] == {}
    assert "configurable" not in config


def test_build_run_config_rejects_non_mapping_context():
    """When caller sends a non-object context, raise a clear error instead of a TypeError."""
    import pytest

    from app.gateway.services import build_run_config

    with pytest.raises(ValueError, match="context"):
        build_run_config("thread-1", {"context": "bad-context"}, None)


def test_build_run_config_null_context_custom_agent_injects_agent_name():
    """Custom assistant_id can still be injected when context=null starts context mode."""
    from app.gateway.services import build_run_config

    config = build_run_config("thread-1", {"context": None}, None, assistant_id="finalis")

    assert config["context"] == {"agent_name": "finalis"}
    assert "configurable" not in config


def test_build_run_config_context_plus_configurable_warns(caplog):
    """When caller sends both 'context' and 'configurable', prefer 'context' and log a warning."""
    import logging

    from app.gateway.services import build_run_config

    with caplog.at_level(logging.WARNING, logger="app.gateway.services"):
        config = build_run_config(
            "thread-1",
            {
                "context": {"user_id": "u-42"},
                "configurable": {"model_name": "gpt-4"},
            },
            None,
        )
    assert "context" in config
    assert config["context"]["user_id"] == "u-42"
    assert "configurable" not in config
    assert any("both 'context' and 'configurable'" in r.message for r in caplog.records)


def test_resolve_run_owner_id_uses_cron_creator_user_id():
    from app.gateway.services import resolve_run_owner_id

    owner = resolve_run_owner_id(
        {
            "scheduler": {
                "job": {
                    "creator_user_id": "cron-user-1",
                }
            }
        }
    )

    assert owner == "cron-user-1"


def test_build_run_config_context_passthrough_other_keys():
    """Non-conflicting keys from request_config are still passed through when context is used."""
    from app.gateway.services import build_run_config

    config = build_run_config(
        "thread-1",
        {"context": {"thread_id": "thread-1"}, "tags": ["prod"]},
        None,
    )
    assert config["context"]["thread_id"] == "thread-1"
    assert "configurable" not in config
    assert config["tags"] == ["prod"]


def test_build_run_config_no_request_config():
    """When request_config is None, fall back to basic configurable with thread_id."""
    from app.gateway.services import build_run_config

    config = build_run_config("thread-abc", None, None)
    assert config["configurable"] == {"thread_id": "thread-abc"}
    assert "context" not in config


@pytest.mark.anyio
async def test_start_cron_run_reuses_existing_record(monkeypatch):
    from app.gateway.services import start_cron_run
    from deerflow.runtime.scheduler.schemas import CronJobFireRecord, CronJobRecord

    existing_run = {"run_id": "run-1", "thread_id": "thread-1", "assistant_id": "lead_agent"}
    find_existing = AsyncMock(return_value=existing_run)
    launch_run = AsyncMock()

    monkeypatch.setattr("app.gateway.services.find_existing_cron_run", find_existing)
    monkeypatch.setattr("app.gateway.services._launch_run", launch_run)

    app = FastAPI()
    app.state.run_store = object()
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "app": app})

    job = CronJobRecord(
        job_id="job-1",
        thread_id="thread-1",
        execution_thread_id="thread-exec-1",
        assistant_id="lead_agent",
        cron="*/5 * * * *",
        timezone="Asia/Shanghai",
        input={"messages": [{"role": "user", "content": "hello"}]},
        metadata={"source": "test"},
        config={"configurable": {"model_name": "gpt-4"}},
        context={"thinking_enabled": True},
        multitask_strategy="enqueue",
        enabled=True,
        next_fire_at=1746500000,
        last_fire_at=None,
        last_run_id=None,
        created_at=1746400000,
        updated_at=1746400000,
    )
    fire = CronJobFireRecord(
        fire_id="fire-1",
        job_id="job-1",
        scheduled_fire_at=1746500000,
        status="claimed",
        claim_owner="worker-a",
        claim_token="token-1",
    )

    reused = await start_cron_run(job, fire, request)

    assert reused.run_id == "run-1"
    find_existing.assert_awaited_once_with(app.state.run_store, "thread-exec-1", "cron:job-1:1746500000")
    launch_run.assert_not_awaited()


@pytest.mark.anyio
async def test_start_cron_run_with_deps_injects_scheduler_metadata(monkeypatch):
    from app.gateway.services import start_cron_run_with_deps
    from deerflow.runtime.scheduler.schemas import CronJobFireRecord, CronJobRecord

    launch_run = AsyncMock(return_value=SimpleNamespace(run_id="run-2"))
    monkeypatch.setattr("app.gateway.services._launch_run", launch_run)

    job = CronJobRecord(
        job_id="job-1",
        thread_id="thread-1",
        execution_thread_id="thread-exec-1",
        assistant_id="lead_agent",
        cron="*/5 * * * *",
        timezone="Asia/Shanghai",
        input={"messages": [{"role": "user", "content": "hello"}]},
        metadata={"source": "test", "scheduler": {"prior": "value"}},
        config={"configurable": {"model_name": "gpt-4"}},
        context={"thinking_enabled": True},
        multitask_strategy="enqueue",
        enabled=True,
        next_fire_at=1746500000,
        last_fire_at=None,
        last_run_id=None,
        created_at=1746400000,
        updated_at=1746400000,
    )
    fire = CronJobFireRecord(
        fire_id="fire-1",
        job_id="job-1",
        scheduled_fire_at=1746500000,
        status="claimed",
        claim_owner="worker-a",
        claim_token="token-1",
    )

    await start_cron_run_with_deps(
        job,
        fire,
        thread_id="thread-1",
        bridge=object(),
        run_mgr=object(),
        run_ctx=object(),
    )

    cron_request = launch_run.await_args.args[0]

    assert cron_request.metadata["source"] == "test"
    assert cron_request.metadata["scheduler"]["job_id"] == "job-1"
    assert cron_request.metadata["scheduler"]["fire_id"] == "fire-1"
    assert cron_request.metadata["scheduler"]["scheduled_fire_at"] == 1746500000
    assert cron_request.metadata["scheduler"]["idempotency_key"] == "cron:job-1:1746500000"
    assert cron_request.metadata["scheduler"]["job"] == {
        "job_id": "job-1",
        "thread_id": "thread-1",
        "execution_thread_id": "thread-exec-1",
        "assistant_id": "lead_agent",
        "cron_expr": "*/5 * * * *",
        "timezone": "Asia/Shanghai",
        "creator_user_id": "default",
        "input": {"messages": [{"role": "user", "content": "hello"}]},
    }
    assert cron_request.metadata["scheduler"]["delivery"] is None
    assert cron_request.metadata["scheduler"]["fire"] == {
        "fire_id": "fire-1",
        "job_id": "job-1",
        "scheduled_fire_at": 1746500000,
    }
    assert cron_request.multitask_strategy == "reject"


@pytest.mark.anyio
async def test_start_cron_run_with_deps_degrades_enqueue_strategy(monkeypatch, caplog):
    from app.gateway.services import start_cron_run_with_deps
    from deerflow.runtime.scheduler.schemas import CronJobFireRecord, CronJobRecord

    launch_run = AsyncMock(return_value=SimpleNamespace(run_id="run-3"))
    monkeypatch.setattr("app.gateway.services._launch_run", launch_run)

    job = CronJobRecord(
        job_id="job-legacy",
        thread_id="thread-1",
        execution_thread_id="thread-exec-legacy",
        assistant_id="lead_agent",
        cron="*/5 * * * *",
        timezone="Asia/Shanghai",
        input={"messages": [{"role": "user", "content": "hello"}]},
        metadata={},
        config=None,
        context=None,
        multitask_strategy="enqueue",
        enabled=True,
        next_fire_at=1746500000,
        last_fire_at=None,
        last_run_id=None,
        created_at=1746400000,
        updated_at=1746400000,
    )
    fire = CronJobFireRecord(
        fire_id="fire-legacy",
        job_id="job-legacy",
        scheduled_fire_at=1746500000,
        status="claimed",
        claim_owner="worker-a",
        claim_token="token-legacy",
    )

    with caplog.at_level("WARNING", logger="app.gateway.services"):
        await start_cron_run_with_deps(
            job,
            fire,
            thread_id="thread-1",
            bridge=object(),
            run_mgr=object(),
            run_ctx=object(),
        )

    cron_request = launch_run.await_args.args[0]
    assert cron_request.multitask_strategy == "reject"
    assert "degrading to 'reject'" in caplog.text


@pytest.mark.anyio
async def test_start_cron_run_with_deps_logs_cron_llm_debug(monkeypatch, caplog):
    from app.gateway.services import start_cron_run_with_deps
    from deerflow.runtime.scheduler.schemas import CronJobFireRecord, CronJobRecord

    monkeypatch.setenv("DEERFLOW_CRON_LLM_DEBUG", "1")
    launch_run = AsyncMock(return_value=SimpleNamespace(run_id="run-debug"))
    monkeypatch.setattr("app.gateway.services._launch_run", launch_run)

    job = CronJobRecord(
        job_id="job-debug",
        thread_id="thread-chat",
        execution_thread_id="thread-exec",
        assistant_id="lead_agent",
        cron="*/5 * * * *",
        timezone="Asia/Shanghai",
        input={"messages": [{"role": "user", "content": "分析昨日数据并输出报告"}]},
        metadata={"source": "test"},
        config={"configurable": {"model_name": "gpt-test"}},
        context={"thinking_enabled": True},
        multitask_strategy="reject",
        enabled=True,
        next_fire_at=1746500000,
        last_fire_at=None,
        last_run_id=None,
        created_at=1746400000,
        updated_at=1746400000,
    )
    fire = CronJobFireRecord(
        fire_id="fire-debug",
        job_id="job-debug",
        scheduled_fire_at=1746500000,
        status="claimed",
        claim_owner="worker-a",
        claim_token="token-debug",
    )

    with caplog.at_level("DEBUG", logger="app.gateway.services"):
        await start_cron_run_with_deps(
            job,
            fire,
            thread_id="thread-exec",
            bridge=object(),
            run_mgr=object(),
            run_ctx=object(),
        )

    assert "Cron LLM debug input" in caplog.text
    assert '"content": "分析昨日数据并输出报告"' in caplog.text
    assert '"execution_thread_id": "thread-exec"' in caplog.text
