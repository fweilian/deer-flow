"""Explicit runtime dependencies for direct ``create_deerflow_agent`` use."""

from __future__ import annotations

from typing import TYPE_CHECKING

from deerflow.config.subagent_runtime_config import SubagentRuntimeConfig
from deerflow.config.subagents_config import (
    DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN,
    MAX_TOTAL_SUBAGENTS_PER_RUN,
    MIN_TOTAL_SUBAGENTS_PER_RUN,
)
from deerflow.subagents.capacity import SubagentExecutionCapacity

if TYPE_CHECKING:
    from deerflow.config.app_config import AppConfig


class SubagentRuntime:
    """Share native-subagent capacity across directly created graphs.

    Application entry points install equivalent process-global dependencies at
    startup. Direct graph factories instead receive this object explicitly, so
    multiple graphs can share one real execution ceiling. Supplying
    ``app_config`` also keeps their subagent registry, model, and tool
    resolution on the same caller-owned snapshot instead of global YAML.

    """

    def __init__(
        self,
        config: SubagentRuntimeConfig | None = None,
        *,
        max_total_per_run: int = DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN,
        app_config: AppConfig | None = None,
    ) -> None:
        if not MIN_TOTAL_SUBAGENTS_PER_RUN <= max_total_per_run <= MAX_TOTAL_SUBAGENTS_PER_RUN:
            raise ValueError(f"max_total_per_run must be between {MIN_TOTAL_SUBAGENTS_PER_RUN} and {MAX_TOTAL_SUBAGENTS_PER_RUN}")
        self.config = (config or SubagentRuntimeConfig()).model_copy(deep=True)
        self.max_total_per_run = max_total_per_run
        self.app_config = app_config
        self.execution_capacity = SubagentExecutionCapacity(self.config)

    @classmethod
    def from_app_config(
        cls,
        app_config: AppConfig,
    ) -> SubagentRuntime:
        """Build explicit SDK dependencies from a caller-owned config snapshot."""

        runtime_config = getattr(app_config, "subagent_runtime", None)
        if not isinstance(runtime_config, SubagentRuntimeConfig):
            runtime_config = SubagentRuntimeConfig()
        max_total_per_run = int(
            getattr(
                getattr(app_config, "subagents", None),
                "max_total_per_run",
                DEFAULT_MAX_TOTAL_SUBAGENTS_PER_RUN,
            )
        )
        return cls(
            runtime_config,
            max_total_per_run=max_total_per_run,
            app_config=app_config,
        )
