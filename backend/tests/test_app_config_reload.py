"""Regression tests for the retained application configuration surface."""

from __future__ import annotations

from deerflow.config.app_config import AppConfig


def test_app_config_excludes_removed_runtime_sections() -> None:
    config = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}})

    for field in ("channel_connections", "mcp_tasks", "subagent_batches", "dedupe_storage"):
        assert not hasattr(config, field)


def test_app_config_keeps_regular_subagent_runtime_configuration() -> None:
    config = AppConfig.model_validate(
        {
            "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
            "subagent_runtime": {"max_running": 3},
        }
    )

    assert config.subagent_runtime.max_running == 3
