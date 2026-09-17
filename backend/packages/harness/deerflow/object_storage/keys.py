"""Stable, validated keys for DeerFlow's shared Phase 5 object store."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")


class ObjectKeyNamespace:
    """Build the only object-key layout used by Phase 5 persistent file data.

    Artifact payloads intentionally remain under ``outputs`` because
    ``.tool-results`` is an internal output area rather than a distinct
    artifact type.  This class has no storage side effects and is safe to use
    at request-validation boundaries.
    """

    def __init__(self, prefix: str = "deer-flow/v1") -> None:
        self._prefix = self._validate_prefix(prefix)

    @property
    def prefix(self) -> str:
        return self._prefix

    def outputs_prefix(self, user_id: str, thread_id: str) -> str:
        return self._join("users", self._identifier(user_id, "user_id"), "threads", self._identifier(thread_id, "thread_id"), "outputs")

    def artifact(self, user_id: str, thread_id: str, relative_path: str) -> str:
        return self._relative(self.outputs_prefix(user_id, thread_id), relative_path)

    def tool_results_prefix(self, user_id: str, thread_id: str) -> str:
        return f"{self.outputs_prefix(user_id, thread_id)}/.tool-results"

    def tool_result(self, user_id: str, thread_id: str, relative_path: str) -> str:
        return self._relative(self.tool_results_prefix(user_id, thread_id), relative_path)

    def uploads_prefix(self, user_id: str, thread_id: str) -> str:
        return self._join("users", self._identifier(user_id, "user_id"), "threads", self._identifier(thread_id, "thread_id"), "uploads")

    def upload(self, user_id: str, thread_id: str, filename: str) -> str:
        return self._relative(self.uploads_prefix(user_id, thread_id), filename)

    def custom_skills_prefix(self, user_id: str) -> str:
        return self._join("users", self._identifier(user_id, "user_id"), "skills", "custom")

    def custom_skill(self, user_id: str, skill_name: str, relative_path: str) -> str:
        return self._relative(f"{self.custom_skills_prefix(user_id)}/{self._identifier(skill_name, 'skill_name')}", relative_path)

    def skill_state(self, user_id: str, skill_name: str) -> str:
        """Return the independently writable enabled-state object for one skill."""
        return f"{self.skill_states_prefix(user_id)}/{self._identifier(skill_name, 'skill_name')}.json"

    def skill_states_prefix(self, user_id: str) -> str:
        return self._join(
            "users",
            self._identifier(user_id, "user_id"),
            "skills",
            "state",
        )

    def _join(self, *parts: str) -> str:
        return "/".join((self._prefix, *parts))

    @staticmethod
    def _identifier(value: str, field: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"{field} must be a non-empty safe object-key identifier.")
        return value

    @staticmethod
    def _validate_prefix(prefix: str) -> str:
        normalized = prefix.strip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("Object key prefix must not be empty or contain traversal segments.")
        return normalized

    @staticmethod
    def _relative(prefix: str, relative_path: str) -> str:
        path = PurePosixPath(relative_path)
        if not relative_path or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("Object relative path must be non-empty and stay under its namespace.")
        return f"{prefix}/{path.as_posix()}"
