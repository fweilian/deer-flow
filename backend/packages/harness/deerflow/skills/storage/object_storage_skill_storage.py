"""Shared-object-storage backend for user-authored skills.

The local custom-skills directory is a disposable parser/export cache only.
Custom packages, enabled state, and history are always read from and written to
the Phase 5 object store.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from deerflow.object_storage import ObjectKeyNamespace, get_object_storage
from deerflow.skills.storage.skill_storage import SKILL_MD_FILE
from deerflow.skills.storage.user_scoped_skill_storage import UserScopedSkillStorage
from deerflow.skills.types import SkillCategory


def _run_shared(coro):
    """Run the async shared-store port from the legacy synchronous contract."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[object] = []
    failure: list[BaseException] = []

    def _runner() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # pragma: no cover - exercised by callers
            failure.append(exc)

    thread = threading.Thread(target=_runner, name="deerflow-skill-storage", daemon=True)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0] if result else None


def _missing_object(error: Exception) -> bool:
    if isinstance(error, FileNotFoundError):
        return True
    response = getattr(error, "response", None)
    return str((response or {}).get("Error", {}).get("Code", "")) in {"404", "NoSuchKey", "NotFound"}


class ObjectStorageSkillStorage(UserScopedSkillStorage):
    """User-scoped skills whose persistent state lives exclusively in S3.

    ``UserScopedSkillStorage`` remains the protocol-compatible local backend.
    This subclass retains its public/legacy discovery rules and filesystem view
    layout, while replacing every mutable custom-skill operation with shared
    object-store operations.
    """

    per_user_only = True
    allow_legacy_custom_fallback = False

    def __init__(self, user_id: str, host_path: str | None = None, container_path: str = "/mnt/skills", app_config=None) -> None:
        if app_config is None:
            raise ValueError("ObjectStorageSkillStorage requires app_config")
        super().__init__(user_id, host_path=host_path, container_path=container_path, app_config=app_config)
        self._storage = get_object_storage(app_config)
        self._namespace = ObjectKeyNamespace(app_config.object_storage.key_prefix)

    @property
    def _prefix(self) -> str:
        return self._namespace.custom_skills_prefix(self._user_id)

    def _key(self, name: str, relative_path: str) -> str:
        name = self.validate_skill_name(name)
        path = PurePosixPath(relative_path)
        if not relative_path or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("relative_path must stay within the custom skill package.")
        return self._namespace.custom_skill(self._user_id, name, path.as_posix())

    def _history_key(self, name: str) -> str:
        return self._key(name, ".history.jsonl")

    async def _read_bytes(self, key: str) -> bytes:
        chunks: list[bytes] = []
        async with self._storage.open_read(key) as read:
            async for chunk in read.chunks:
                chunks.append(chunk)
        return b"".join(chunks)

    async def _write_bytes(self, key: str, payload: bytes, *, content_type: str | None = None) -> None:
        async def _chunks() -> AsyncIterator[bytes]:
            yield payload

        await self._storage.write_stream(key, _chunks(), content_type=content_type)

    async def _delete_prefix(self, prefix: str) -> None:
        keys = [metadata.key async for metadata in self._storage.list_prefix(prefix)]
        for key in keys:
            await self._storage.delete(key)

    async def _materialize_cache(self) -> None:
        """Rebuild the local, non-authoritative custom-skill cache from S3."""
        prefix = f"{self._prefix}/"
        objects = [metadata async for metadata in self._storage.list_prefix(prefix)]
        signature = tuple((metadata.key, metadata.size, metadata.etag, metadata.last_modified.isoformat() if metadata.last_modified else None) for metadata in objects)
        if getattr(self, "_cache_signature", None) == signature and self._user_custom_root.is_dir():
            return
        with tempfile.TemporaryDirectory(prefix="deerflow-skills-cache-") as tmp:
            staging = Path(tmp) / "custom"
            staging.mkdir()
            for metadata in objects:
                if not metadata.key.startswith(prefix):
                    raise ValueError("Object storage returned a key outside the custom-skills namespace.")
                relative = PurePosixPath(metadata.key[len(prefix) :])
                if len(relative.parts) < 2 or any(part in {"", ".", ".."} for part in relative.parts):
                    raise ValueError("Object storage returned an invalid custom-skill key.")
                name, *parts = relative.parts
                self.validate_skill_name(name)
                if parts == [".history.jsonl"]:
                    target = staging / ".history" / f"{name}.jsonl"
                else:
                    target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                async with self._storage.open_read(metadata.key) as read:
                    with target.open("wb") as handle:
                        async for chunk in read.chunks:
                            handle.write(chunk)
                if metadata.last_modified is not None:
                    timestamp = metadata.last_modified.timestamp()
                    os.utime(target, (timestamp, timestamp))

            cache_root = self._user_custom_root
            cache_root.parent.mkdir(parents=True, exist_ok=True)
            replacement = cache_root.parent / f".{cache_root.name}.next"
            if replacement.exists():
                shutil.rmtree(replacement)
            shutil.copytree(staging, replacement)
            if cache_root.exists():
                shutil.rmtree(cache_root)
            replacement.replace(cache_root)
        self._cache_signature = signature

    def _refresh_cache(self) -> None:
        _run_shared(self._materialize_cache())

    def load_skills(self, *, enabled_only: bool = False) -> list:
        self._refresh_cache()
        return super().load_skills(enabled_only=enabled_only)

    def custom_skill_exists(self, name: str) -> bool:
        try:
            _run_shared(self._storage.stat(self._key(name, SKILL_MD_FILE)))
        except Exception as exc:
            if _missing_object(exc):
                return False
            raise
        return True

    def read_custom_skill(self, name: str) -> str:
        try:
            return _run_shared(self._read_bytes(self._key(name, SKILL_MD_FILE))).decode("utf-8")
        except Exception as exc:
            if _missing_object(exc):
                raise FileNotFoundError(f"Custom skill '{name}' not found.") from exc
            raise

    def get_custom_skill_dir(self, name: str) -> Path:
        self._refresh_cache()
        return self._user_custom_root / self.validate_skill_name(name)

    def get_custom_skill_file(self, name: str) -> Path:
        return self.get_custom_skill_dir(name) / SKILL_MD_FILE

    def get_skill_history_file(self, name: str) -> Path:
        self._refresh_cache()
        return self._user_custom_root / ".history" / f"{self.validate_skill_name(name)}.jsonl"

    def write_custom_skill(self, name: str, relative_path: str, content: str) -> None:
        key = self._key(name, relative_path)
        with self._skill_projection_mutation():
            _run_shared(self._write_bytes(key, content.encode("utf-8"), content_type="text/plain; charset=utf-8"))

    def remove_custom_skill_file(self, name: str, relative_path: str) -> str:
        key = self._key(name, relative_path)
        with self._skill_projection_mutation(remove=((SkillCategory.CUSTOM, Path(name)),)):
            try:
                previous = _run_shared(self._read_bytes(key)).decode("utf-8")
            except Exception as exc:
                if _missing_object(exc):
                    raise FileNotFoundError(f"Supporting file '{relative_path}' not found for skill '{name}'.") from exc
                raise
            _run_shared(self._storage.delete(key))
            return previous

    def delete_custom_skill(self, name: str, *, history_meta: dict | None = None) -> None:
        name = self.validate_skill_name(name)
        self.ensure_custom_skill_is_editable(name)
        if history_meta is not None:
            self.append_history(name, {**history_meta, "prev_content": self.read_custom_skill(name)})
        with self._skill_projection_mutation(remove=((SkillCategory.CUSTOM, Path(name)),)):
            _run_shared(self._delete_prefix(f"{self._prefix}/{name}/"))
            # Enabled state is an independently writable shared object, so a
            # deleted name cannot leak state into a later recreation.
            _run_shared(self._storage.delete(self._namespace.skill_state(self._user_id, name)))

    def get_skill_enabled_state(self, skill_name: str) -> bool:
        try:
            raw = _run_shared(self._read_bytes(self._namespace.skill_state(self._user_id, skill_name)))
        except Exception as exc:
            if _missing_object(exc):
                return True
            raise
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("enabled", True), bool):
            raise ValueError("Shared skill state must contain a boolean enabled value.")
        return data.get("enabled", True)

    def _read_skill_states(self) -> dict[str, dict[str, bool]]:
        """Expose a projection manifest without reintroducing a shared state blob."""
        prefix = f"{self._namespace.skill_states_prefix(self._user_id)}/"

        async def _read_all() -> dict[str, dict[str, bool]]:
            states: dict[str, dict[str, bool]] = {}
            async for metadata in self._storage.list_prefix(prefix):
                if not metadata.key.startswith(prefix) or not metadata.key.endswith(".json"):
                    raise ValueError("Object storage returned an invalid skill state key.")
                name = metadata.key[len(prefix) : -len(".json")]
                self.validate_skill_name(name)
                raw = await self._read_bytes(metadata.key)
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("enabled", True), bool):
                    raise ValueError("Shared skill state must contain a boolean enabled value.")
                states[name] = {"enabled": data.get("enabled", True)}
            return states

        return _run_shared(_read_all())

    def set_skill_enabled_state(self, skill_name: str, enabled: bool) -> None:
        name = self.validate_skill_name(skill_name)
        removal_names = (name,) if not enabled else ()
        with self._skill_projection_mutation(remove_names=removal_names):
            # One object per skill avoids cross-instance read-modify-write
            # conflicts without introducing a distributed lock.
            payload = json.dumps({"enabled": enabled}, ensure_ascii=False).encode("utf-8")
            _run_shared(self._write_bytes(self._namespace.skill_state(self._user_id, name), payload, content_type="application/json"))

    def append_history(self, name: str, record: dict) -> None:
        name = self.validate_skill_name(name)
        key = self._history_key(name)
        try:
            existing = _run_shared(self._read_bytes(key)).decode("utf-8")
        except Exception as exc:
            if not _missing_object(exc):
                raise
            existing = ""
        payload = {"ts": datetime.now(UTC).isoformat(), **record}
        _run_shared(self._write_bytes(key, (existing + json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"), content_type="application/x-ndjson"))

    def read_history(self, name: str) -> list[dict]:
        try:
            payload = _run_shared(self._read_bytes(self._history_key(name))).decode("utf-8")
        except Exception as exc:
            if _missing_object(exc):
                return []
            raise
        return [json.loads(line) for line in payload.splitlines() if line.strip()]

    async def ainstall_skill_from_archive(self, archive_path: str | Path) -> dict:
        from deerflow.skills.installer import SkillAlreadyExistsError, _scan_skill_archive_contents_or_raise

        archive = Path(archive_path)
        with tempfile.TemporaryDirectory(prefix="deerflow-skill-install-") as tmp:
            root = Path(tmp)
            extract_root = root / "extract"
            extract_root.mkdir()
            skill_dir, skill_name, _target = await asyncio.to_thread(self._prepare_skill_archive, archive, extract_root, root / "custom", archive_path)
            if self.custom_skill_exists(skill_name):
                raise SkillAlreadyExistsError(f"Skill '{skill_name}' already exists")
            await _scan_skill_archive_contents_or_raise(skill_dir, skill_name, app_config=self._app_config)
            await asyncio.to_thread(self._write_package, skill_name, skill_dir)
        return {"success": True, "skill_name": skill_name, "message": f"Skill '{skill_name}' installed successfully"}

    def _write_package(self, name: str, source: Path) -> None:
        with self._skill_projection_mutation():
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(source).as_posix()
                    _run_shared(self._write_bytes(self._key(name, relative), path.read_bytes()))
