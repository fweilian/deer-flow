"""Configuration and loaders for custom agents.

Custom agents are stored per-user under ``{base_dir}/users/{user_id}/agents/{name}/``.
A legacy shared layout at ``{base_dir}/agents/{name}/`` is still readable so that
installations that pre-date user isolation continue to work until they run the
``scripts/migrate_user_isolation.py`` migration. New writes always target the
per-user layout.
"""

import logging
import re
import unicodedata
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import get_effective_user_id

logger = logging.getLogger(__name__)

SOUL_FILENAME = "SOUL.md"
AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
MAX_AGENT_OUTPUT_TOKENS = 200_000


def _validate_display_name(value: object) -> object:
    # Check before trimming so leading/trailing controls cannot disappear.
    # Keep ordinary RTL text, ZWNJ in Persian/Indic text and ZWJ in emoji.
    if isinstance(value, str):
        if re.search(r"[\x00-\x1f\x7f-\x9f\u00ad\u061c\u200b\u200e-\u200f\u2028-\u202e\u2060-\u2069\ufeff]", value):
            raise ValueError("Display name must not contain control characters or invisible formatting controls")
        if value.strip() and all(unicodedata.category(char)[0] in {"C", "M", "Z"} for char in value):
            raise ValueError("Display name must contain visible text")
    return value


AgentDisplayName = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100), BeforeValidator(_validate_display_name)]


def validate_agent_name(name: str | None) -> str | None:
    """Validate a custom agent name before using it in filesystem paths."""
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValueError("Invalid agent name. Expected a string or None.")
    if not AGENT_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Invalid agent name '{name}'. Must match pattern: {AGENT_NAME_PATTERN.pattern}")
    return name


class AgentModelSettings(BaseModel):
    """Per-agent LLM sampling overrides layered on top of the model profile.

    These are provider sampling knobs (not DeerFlow runtime switches like
    ``thinking_enabled``). They let two agents that reference the *same*
    ``models:`` profile still run with different temperature / output length —
    the core ask of issue #4336, where "different agents have different
    capabilities, so a shared temperature is a poor fit".

    ``extra="forbid"``: the sampling surface is an explicit allowlist so a
    stray key never reaches the provider request body and fails at request
    time with an opaque error. Widen it by adding a declared field (e.g.
    ``top_p``) rather than relaxing the model config. Every field is optional;
    ``None`` means "do not override the profile value".
    """

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="Sampling temperature override (0.0-2.0). None = inherit the model profile's value.",
    )
    max_tokens: int | None = Field(
        default=None,
        ge=1,
        le=MAX_AGENT_OUTPUT_TOKENS,
        description=f"Max output tokens override (1-{MAX_AGENT_OUTPUT_TOKENS}). None = inherit the model profile's value.",
    )


class AgentConfig(BaseModel):
    """Configuration for a custom agent."""

    name: str
    display_name: AgentDisplayName | None = None
    description: str = ""
    model: str | None = None
    tool_groups: list[str] | None = None
    # skills controls which skills are discoverable and may be activated by the
    # agent. It does not activate their allowed-tools policies at construction:
    # - None (or omitted): load all enabled skills (default fallback behavior)
    # - [] (explicit empty list): disable all skills
    # - ["skill1", "skill2"]: load only the specified skills
    skills: list[str] | None = None
    # Controls which deployment-level subagents this custom agent may invoke:
    # None = all currently enabled definitions, [] = none, list = allowlist.
    # The default Lead Agent has no AgentConfig and therefore keeps access to
    # the full catalog.
    allowed_subagents: list[str] | None = None
    # Per-agent LLM sampling overrides (temperature / max_tokens) layered on top
    # of the referenced model profile. None = no overrides (issue #4336).
    model_settings: AgentModelSettings | None = None
    # Per-agent thinking-mode default. None = do not override the runtime
    # default (a request-supplied thinking flag still wins over this).
    thinking_enabled: bool | None = None
    # Per-agent reasoning-effort default for models that support it. None = do
    # not override (a request-supplied reasoning_effort still wins over this).
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    # Disable every memory path for stateless execution-oriented agents while
    # preserving the global memory configuration for all other agents.
    memory_enabled: bool = True


# Fields explicitly managed by agent-update surfaces. Anything else declared
# on :class:`AgentConfig` — and any future field — is
# preserved verbatim by :func:`preserve_non_managed_fields` so update surfaces
# do not silently drop hand-authored configuration. Some surfaces expose only a
# subset of these managed fields (for example, the harness ``update_agent``
# tool does not accept model-behavior arguments), so they must carry their
# unsupported managed fields forward explicitly when rewriting config.yaml.
# ``name`` is included because updaters always re-emit it from the directory
# name (it must never come from the request body).
MANAGED_AGENT_CONFIG_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "model",
        "tool_groups",
        "skills",
        "allowed_subagents",
        "model_settings",
        "thinking_enabled",
        "reasoning_effort",
    }
)


def preserve_non_managed_fields(existing_cfg: AgentConfig) -> dict[str, object]:
    """Return every top-level field on ``existing_cfg`` not in :data:`MANAGED_AGENT_CONFIG_FIELDS`.

    Used by the two surfaces that rewrite a custom agent's ``config.yaml``
    (the ``update_agent`` harness tool and the HTTP ``PATCH /api/agents/{name}``
    route) to carry forward any hand-authored field that the
    update API does not expose as an argument. Without this, operators who
    future hand-authored fields survive the next agent or UI edit.

    ``exclude_unset=True`` is recursive in Pydantic v2, so a sub-field the
    user did not write (and that defaulted to a Pydantic default) is not
    materialized into the dict — the file round-trips visually intact.
    """
    return existing_cfg.model_dump(exclude_unset=True, exclude=MANAGED_AGENT_CONFIG_FIELDS)


def resolve_agent_dir(name: str, *, user_id: str | None = None) -> Path:
    """Return the on-disk directory for an agent, preferring the per-user layout.

    Resolution order:
    1. ``{base_dir}/users/{user_id}/agents/{name}/`` (per-user, current layout).
    2. ``{base_dir}/agents/{name}/`` (legacy shared layout — read-only fallback).

    If neither exists, the per-user path is returned so callers that intend to
    create the agent write into the new layout.

    Args:
        name: Validated agent name.
        user_id: Owner of the agent. Defaults to the effective user from the
            request context (or ``"default"`` in no-auth mode).
    """
    paths = get_paths()
    effective_user = user_id or get_effective_user_id()
    user_path = paths.user_agent_dir(effective_user, name)
    # Require config.yaml to confirm this is a genuine agent directory,
    # not a leftover from memory/storage writes (see #3390).
    if user_path.exists() and (user_path / "config.yaml").exists():
        return user_path

    legacy_path = paths.agent_dir(name)
    if legacy_path.exists() and (legacy_path / "config.yaml").exists():
        return legacy_path

    return user_path


def load_agent_config(name: str | None, *, user_id: str | None = None) -> AgentConfig | None:
    """Load the custom or default agent's config.

    Dispatches to the configured agent store (``agent_storage.backend``): the
    ``file`` backend reads the per-user layout first and falls back to the legacy
    shared layout; the ``db`` backend reads the shared ``agents`` table. Behaviour
    and error semantics are unchanged from the historical file-only loader.

    Args:
        name: The agent name.
        user_id: Owner of the agent. Defaults to the effective user from the
            current request context.

    Returns:
        AgentConfig instance, or ``None`` if ``name`` is ``None``.

    Raises:
        FileNotFoundError: If the agent does not exist.
        ValueError: If the stored config cannot be parsed.
    """
    if name is None:
        return None
    # Lazy import: the store package imports back from this module.
    from deerflow.persistence.agents import get_agent_store

    return get_agent_store().get(name, user_id=user_id)


def load_agent_soul(agent_name: str | None, *, user_id: str | None = None) -> str | None:
    """Read the SOUL.md content for an agent, if any.

    SOUL.md defines the agent's personality, values, and behavioral guardrails.
    It is injected into the lead agent's system prompt as additional context.
    The default agent (``agent_name`` falsy) always reads ``{base_dir}/SOUL.md``
    directly — it is not a custom-agent record — regardless of backend. A named
    agent dispatches to the configured store.

    Args:
        agent_name: The name of the agent or None for the default agent.
        user_id: Owner of the agent. Defaults to the effective user from the
            current request context.

    Returns:
        The SOUL.md content as a string, or None if not set.
    """
    if not agent_name:
        soul_path = get_paths().base_dir / SOUL_FILENAME
        if not soul_path.exists():
            return None
        content = soul_path.read_text(encoding="utf-8").strip()
        return content or None
    from deerflow.persistence.agents import get_agent_store

    return get_agent_store().get_soul(agent_name, user_id=user_id)


def list_custom_agents(*, user_id: str | None = None) -> list[AgentConfig]:
    """Return all valid custom agents for ``user_id``.

    Dispatches to the configured agent store. The ``file`` backend returns the
    union of the per-user layout and the legacy shared layout (per-user entries
    shadow legacy entries with the same name); the ``db`` backend returns the
    user's rows. Sorted by name.

    Args:
        user_id: Owner whose agents to list. Defaults to the effective user
            from the current request context.

    Returns:
        List of AgentConfig for each valid agent found.
    """
    from deerflow.persistence.agents import get_agent_store

    return get_agent_store().list(user_id=user_id)
