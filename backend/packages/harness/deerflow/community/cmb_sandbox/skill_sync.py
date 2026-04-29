from __future__ import annotations

import logging
import os
import shlex
import tempfile
import threading
import zipfile
from pathlib import Path

from deerflow.config import get_app_config
from deerflow.skills.loader import load_skills

from .client import CmbSandboxClient

logger = logging.getLogger(__name__)


class CmbSkillSyncManager:
    """Detects referenced skills and syncs them into CMB sandbox on demand."""

    def __init__(
        self,
        client: CmbSandboxClient,
        *,
        skills_container_path: str,
        sandbox_root: str,
    ):
        self._client = client
        self._skills_container_path = skills_container_path.rstrip("/") or "/mnt/skills"
        self._sandbox_skills_dir = f"{sandbox_root.rstrip('/')}/skills"

        self._sync_lock = threading.RLock()
        self._uploaded_skills: set[str] = set()
        self._container_to_local = self._build_skill_index()

    def _build_skill_index(self) -> dict[str, Path]:
        skill_index: dict[str, Path] = {}
        for skill in load_skills(enabled_only=False):
            container_path = skill.get_container_path(self._skills_container_path)
            skill_index[container_path] = skill.skill_dir
        return skill_index

    def sync_for_text(self, text: str) -> None:
        """Sync all skills whose container paths are referenced by *text*."""
        if not text:
            return
        for container_path in self._iter_referenced_skills(text):
            self._sync_skill_to_sandbox(container_path)

    def _iter_referenced_skills(self, text: str):
        for container_path in sorted(self._container_to_local, key=len, reverse=True):
            if container_path in text:
                yield container_path

    def _sync_skill_to_sandbox(self, container_path: str) -> bool:
        if container_path in self._uploaded_skills:
            return True

        with self._sync_lock:
            if container_path in self._uploaded_skills:
                return True

            local_path = self._container_to_local.get(container_path)
            if local_path is None:
                logger.warning("Unknown skill path in sync request: %s", container_path)
                return False
            if not local_path.exists():
                logger.warning("Skill path no longer exists: %s", local_path)
                return False

            logger.debug("Syncing skill %s from %s", container_path, local_path)

            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_zip:
                zip_path = Path(tmp_zip.name)

            try:
                self._create_zip_archive(local_path, zip_path)

                if not self._client.upload_file(str(zip_path), "skills"):
                    return False

                remote_zip_path = f"{self._sandbox_skills_dir}/{zip_path.name}"
                command = (
                    f"unzip -o {shlex.quote(remote_zip_path)} -d {shlex.quote(self._sandbox_skills_dir)} "
                    f"&& rm {shlex.quote(remote_zip_path)}"
                )
                output, code = self._client.execute_command(command)

                if code != 0:
                    logger.error("Failed to unzip synced skill %s: %s", container_path, output)
                    return False

                self._uploaded_skills.add(container_path)
                return True
            except Exception as exc:
                logger.error("Skill sync failed for %s: %s", container_path, exc, exc_info=True)
                return False
            finally:
                try:
                    os.remove(zip_path)
                except OSError:
                    pass

    @staticmethod
    def _create_zip_archive(local_skill_path: Path, zip_path: Path) -> None:
        skills_root = get_app_config().skills.get_skills_path()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for path in local_skill_path.rglob("*"):
                if not path.is_file():
                    continue

                try:
                    archive_path = path.relative_to(skills_root)
                except ValueError:
                    archive_path = Path(local_skill_path.name) / path.relative_to(local_skill_path)

                zip_file.write(path, archive_path.as_posix())
