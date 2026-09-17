"""Focused admission coverage for Phase 5 production storage requirements."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.gateway import phase5_gate

_SHARED_SKILLS = "deerflow.skills.storage.object_storage_skill_storage:ObjectStorageSkillStorage"


def _config(*, storage_enabled: bool = True, skills_use: str = _SHARED_SKILLS):
    return SimpleNamespace(
        object_storage=SimpleNamespace(enabled=storage_enabled, bucket="deer-flow" if storage_enabled else None, key_prefix="deer-flow/v1"),
        skills=SimpleNamespace(use=skills_use),
    )


def test_production_gate_rejects_local_phase5_storage_and_missing_jwt_secret(monkeypatch) -> None:
    monkeypatch.setenv("DEER_FLOW_ENV", "production")
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="object_storage.enabled=true.*ObjectStorageSkillStorage.*AUTH_JWT_SECRET"):
        phase5_gate.validate_production_phase5_configuration(_config(storage_enabled=False, skills_use="deerflow.skills.storage.local_skill_storage:LocalSkillStorage"))


def test_production_gate_accepts_shared_storage_and_explicit_jwt_secret(monkeypatch) -> None:
    monkeypatch.setenv("DEER_FLOW_ENV", "production")
    monkeypatch.setenv("AUTH_JWT_SECRET", "production-test-secret")

    phase5_gate.validate_production_phase5_configuration(_config())


def test_production_gate_probes_shared_storage_before_starting(monkeypatch) -> None:
    monkeypatch.setenv("DEER_FLOW_ENV", "production")

    class UnavailableStorage:
        async def list_prefix(self, _prefix):
            raise OSError("bucket unavailable")
            yield  # pragma: no cover

    monkeypatch.setattr(phase5_gate, "get_object_storage", lambda _config: UnavailableStorage())
    with pytest.raises(OSError, match="bucket unavailable"):
        asyncio.run(phase5_gate.verify_production_phase5_storage(_config()))
