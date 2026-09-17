"""Production admission checks for Phase 5 shared file storage."""

from __future__ import annotations

import os

from app.gateway.auth_disabled import is_explicit_production_environment
from deerflow.object_storage import ObjectKeyNamespace, get_object_storage

_OBJECT_STORAGE_SKILL_BACKEND = "deerflow.skills.storage.object_storage_skill_storage:ObjectStorageSkillStorage"


def validate_production_phase5_configuration(app_config) -> None:
    """Reject production settings that would reintroduce node-local Phase 5 data."""
    if not is_explicit_production_environment():
        return

    errors: list[str] = []
    storage = app_config.object_storage
    if not storage.enabled or not storage.bucket:
        errors.append("object_storage.enabled=true with object_storage.bucket is required")
    if app_config.skills.use != _OBJECT_STORAGE_SKILL_BACKEND:
        errors.append("skills.use must select ObjectStorageSkillStorage for shared custom skills")
    if not os.environ.get("AUTH_JWT_SECRET", "").strip():
        errors.append("AUTH_JWT_SECRET must be explicitly configured")
    if errors:
        raise RuntimeError("Phase 5 production requirements not met: " + "; ".join(errors))


async def verify_production_phase5_storage(app_config) -> None:
    """Probe the configured bucket at startup, before any local fallback is possible."""
    if not is_explicit_production_environment():
        return

    storage = get_object_storage(app_config)
    prefix = ObjectKeyNamespace(app_config.object_storage.key_prefix).prefix + "/"
    # Opening the listing confirms credentials, endpoint reachability, and
    # bucket access without creating application data or assuming it is empty.
    async for _ in storage.list_prefix(prefix):
        break
