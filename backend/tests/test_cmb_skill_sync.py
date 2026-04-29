from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from deerflow.community.cmb_sandbox.skill_sync import CmbSkillSyncManager


class _FakeSkill:
    def __init__(self, skill_dir: Path):
        self.skill_dir = skill_dir

    def get_container_path(self, container_base_path: str = "/mnt/skills") -> str:
        return f"{container_base_path}/public/demo"


class _FakeClient:
    def __init__(self):
        self.upload_calls: list[tuple[str, str, str | None]] = []
        self.zip_entries: list[str] = []
        self.commands: list[str] = []

    def upload_file(self, local_file_path: str, target_path: str, *, filename_override: str | None = None) -> bool:
        import zipfile

        self.upload_calls.append((local_file_path, target_path, filename_override))
        with zipfile.ZipFile(local_file_path, "r") as archive:
            self.zip_entries.extend(archive.namelist())
        return True

    def execute_command(self, command: str):
        self.commands.append(command)
        return "", 0


def test_skill_sync_uploads_once_and_keeps_container_layout(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "public" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")

    fake_client = _FakeClient()

    monkeypatch.setattr(
        "deerflow.community.cmb_sandbox.skill_sync.load_skills",
        lambda enabled_only=False: [_FakeSkill(skill_dir)],
    )
    monkeypatch.setattr(
        "deerflow.community.cmb_sandbox.skill_sync.get_app_config",
        lambda: SimpleNamespace(skills=SimpleNamespace(get_skills_path=lambda: skills_root)),
    )

    manager = CmbSkillSyncManager(
        fake_client,
        skills_container_path="/mnt/skills",
        sandbox_root="/opt/sandbox/file",
    )

    manager.sync_for_text("cat /mnt/skills/public/demo/SKILL.md")
    manager.sync_for_text("ls /mnt/skills/public/demo/scripts")

    assert len(fake_client.upload_calls) == 1
    assert any(entry.endswith("public/demo/SKILL.md") for entry in fake_client.zip_entries)
    assert any(entry.endswith("public/demo/scripts/run.sh") for entry in fake_client.zip_entries)
    assert len(fake_client.commands) == 1
    assert "unzip -o" in fake_client.commands[0]
