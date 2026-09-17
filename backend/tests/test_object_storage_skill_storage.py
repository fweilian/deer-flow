"""Contract coverage for the shared custom-skill storage backend."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from deerflow.config.paths import Paths
from deerflow.object_storage import ObjectMetadata, ObjectRead
from deerflow.skills.projection import ensure_skill_projections
from deerflow.skills.storage import get_or_new_user_skill_storage, reset_skill_storage
from deerflow.skills.storage.object_storage_skill_storage import ObjectStorageSkillStorage
from deerflow.skills.types import SkillCategory


class _MemoryObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.modified: dict[str, datetime] = {}

    async def write_stream(self, key, chunks, *, content_type=None, metadata=None):
        self.objects[key] = b"".join([chunk async for chunk in chunks])
        self.modified[key] = datetime.now(UTC)
        return self._metadata(key)

    @asynccontextmanager
    async def open_read(self, key, *, byte_range=None):
        if key not in self.objects:
            raise FileNotFoundError(key)

        async def _chunks():
            yield self.objects[key]

        yield ObjectRead(metadata=self._metadata(key), chunks=_chunks())

    async def stat(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self._metadata(key)

    async def list_prefix(self, prefix):
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield self._metadata(key)

    async def delete(self, key):
        self.objects.pop(key, None)
        self.modified.pop(key, None)

    def _metadata(self, key: str) -> ObjectMetadata:
        return ObjectMetadata(key, len(self.objects[key]), None, None, self.modified[key], None, {})


def _skill_content(name: str, description: str = "shared demo") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n"


@pytest.fixture
def storage_factory(tmp_path: Path, monkeypatch):
    shared = _MemoryObjectStorage()
    paths = Paths(base_dir=tmp_path / "runtime")
    public_root = tmp_path / "skills"
    (public_root / "public").mkdir(parents=True)
    config = SimpleNamespace(
        object_storage=SimpleNamespace(key_prefix="deer-flow/test"),
        skills=SimpleNamespace(
            get_skills_path=lambda: public_root,
            container_path="/mnt/skills",
            use="deerflow.skills.storage.object_storage_skill_storage:ObjectStorageSkillStorage",
        ),
    )
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    monkeypatch.setattr("deerflow.skills.storage.object_storage_skill_storage.get_object_storage", lambda _config: shared)

    def _make(user_id: str) -> ObjectStorageSkillStorage:
        return ObjectStorageSkillStorage(user_id, host_path=str(public_root), app_config=config)

    return _make, shared, paths, config


def test_custom_skill_and_enabled_state_are_shared_across_instances(storage_factory):
    make_storage, shared, _paths, _config = storage_factory
    writer = make_storage("alice")
    reader = make_storage("alice")

    writer.write_custom_skill("shared-skill", "SKILL.md", _skill_content("shared-skill"))
    writer.write_custom_skill("shared-skill", "references/guide.md", "shared reference")
    writer.set_skill_enabled_state("shared-skill", False)

    skill = next(item for item in reader.load_skills(enabled_only=False) if item.name == "shared-skill")
    assert skill.category == SkillCategory.CUSTOM
    assert skill.enabled is False
    assert reader.read_custom_skill("shared-skill") == _skill_content("shared-skill")
    assert reader.get_custom_skill_dir("shared-skill").joinpath("references/guide.md").read_text() == "shared reference"
    assert any(key.endswith("/skills/state/shared-skill.json") for key in shared.objects)
    assert not any(path.is_file() for path in reader._user_custom_root.rglob("*") if path.name == "SKILL.md" and path.read_text() != _skill_content("shared-skill"))


def test_projection_rebuilds_from_shared_source_after_local_cache_is_removed(storage_factory):
    make_storage, _shared, paths, _config = storage_factory
    writer = make_storage("alice")
    reader = make_storage("alice")
    writer.write_custom_skill("portable-skill", "SKILL.md", _skill_content("portable-skill"))

    import shutil

    shutil.rmtree(reader._user_custom_root)
    projection = ensure_skill_projections(reader)

    assert (projection.custom / "portable-skill" / "SKILL.md").read_text(encoding="utf-8") == _skill_content("portable-skill")
    assert paths.user_custom_skills_dir("alice") != projection.custom


def test_shared_delete_does_not_leave_the_local_cache_as_a_fallback(storage_factory):
    make_storage, shared, _paths, _config = storage_factory
    writer = make_storage("alice")
    reader = make_storage("alice")
    writer.write_custom_skill("deleted-skill", "SKILL.md", _skill_content("deleted-skill"))
    writer.set_skill_enabled_state("deleted-skill", False)
    assert reader.get_custom_skill_file("deleted-skill").exists()

    writer.delete_custom_skill("deleted-skill")

    assert not reader.custom_skill_exists("deleted-skill")
    assert not reader.get_custom_skill_file("deleted-skill").exists()
    assert not any("/deleted-skill/" in key for key in shared.objects)
    assert "deleted-skill" not in reader._read_skill_states()


def test_skill_states_are_independently_writable_shared_objects(storage_factory):
    make_storage, shared, _paths, _config = storage_factory
    first = make_storage("alice")
    second = make_storage("alice")

    first.set_skill_enabled_state("first", False)
    second.set_skill_enabled_state("second", False)

    assert first.get_skill_enabled_state("first") is False
    assert first.get_skill_enabled_state("second") is False
    assert {key.rsplit("/", 1)[-1] for key in shared.objects if "/skills/state/" in key} == {"first.json", "second.json"}


def test_shared_backend_does_not_discover_local_legacy_custom_skills(storage_factory):
    make_storage, _shared, _paths, config = storage_factory
    legacy = config.skills.get_skills_path() / "custom" / "local-only"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(_skill_content("local-only"), encoding="utf-8")

    assert all(skill.name != "local-only" for skill in make_storage("alice").load_skills())


def test_reflection_factory_selects_the_shared_backend_for_user_skills(storage_factory):
    _make_storage, _shared, _paths, config = storage_factory
    reset_skill_storage()
    try:
        storage = get_or_new_user_skill_storage("alice", app_config=config)
        assert isinstance(storage, ObjectStorageSkillStorage)
    finally:
        reset_skill_storage()


def test_shared_read_failure_never_uses_an_existing_local_cache(storage_factory):
    make_storage, shared, _paths, _config = storage_factory
    writer = make_storage("alice")
    reader = make_storage("alice")
    writer.write_custom_skill("no-fallback", "SKILL.md", _skill_content("no-fallback"))
    assert reader.get_custom_skill_file("no-fallback").exists()

    async def _unavailable(_prefix):
        raise RuntimeError("object store unavailable")
        yield  # pragma: no cover

    shared.list_prefix = _unavailable
    with pytest.raises(RuntimeError, match="object store unavailable"):
        reader.load_skills()


@pytest.mark.asyncio
async def test_archive_install_persists_the_entire_custom_package_in_shared_storage(storage_factory, monkeypatch, tmp_path: Path):
    make_storage, _shared, _paths, _config = storage_factory
    archive = tmp_path / "archive-skill.skill"
    with ZipFile(archive, "w") as package:
        package.writestr("archive-skill/SKILL.md", _skill_content("archive-skill"))
        package.writestr("archive-skill/references/guide.md", "shared guide")

    async def _allow_scan(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("deerflow.skills.installer._scan_skill_archive_contents_or_raise", _allow_scan)
    await make_storage("alice").ainstall_skill_from_archive(archive)

    reader = make_storage("alice")
    assert reader.read_custom_skill("archive-skill") == _skill_content("archive-skill")
    assert reader.get_custom_skill_dir("archive-skill").joinpath("references/guide.md").read_text() == "shared guide"
