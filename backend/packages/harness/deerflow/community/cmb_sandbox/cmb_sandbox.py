from __future__ import annotations

import re
import shlex
import threading
from pathlib import PurePosixPath

from deerflow.config import get_app_config
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.search import GrepMatch, path_matches, should_ignore_path, truncate_line

from .client import CmbSandboxClient
from .skill_sync import CmbSkillSyncManager

_SANDBOX_ROOT = "/opt/sandbox/file"


class CmbSandbox(Sandbox):
    """Sandbox implementation backed by CMB HTTP APIs."""

    def __init__(
        self,
        id: str,
        thread_id: str,
        *,
        sandbox_root: str = _SANDBOX_ROOT,
        client: CmbSandboxClient | None = None,
    ):
        super().__init__(id)
        self._thread_id = thread_id
        self._sandbox_root = sandbox_root.rstrip("/")

        skills_container_path = get_app_config().skills.container_path.rstrip("/") or "/mnt/skills"
        self._skills_container_path = skills_container_path

        self._client = client or CmbSandboxClient(thread_id=thread_id)
        self._lock = threading.Lock()

        self._skill_sync = CmbSkillSyncManager(
            self._client,
            skills_container_path=self._skills_container_path,
            sandbox_root=self._sandbox_root,
        )

        self._virtual_to_real = self._build_virtual_to_real_mapping()
        self._real_to_virtual = tuple(
            sorted(((real, virtual) for virtual, real in self._virtual_to_real), key=lambda pair: len(pair[0]), reverse=True)
        )

    def _build_virtual_to_real_mapping(self) -> tuple[tuple[str, str], ...]:
        return (
            ("/mnt/user-data/workspace", f"{self._sandbox_root}/workspace"),
            ("/mnt/user-data/uploads", f"{self._sandbox_root}/uploads"),
            ("/mnt/user-data/outputs", f"{self._sandbox_root}/outputs"),
            ("/mnt/user-data", self._sandbox_root),
            (self._skills_container_path, f"{self._sandbox_root}/skills"),
            ("/mnt/acp-workspace", f"{self._sandbox_root}/acp-workspace"),
        )

    def _map_virtual_path(self, path: str) -> str:
        normalized = path.rstrip("/") if path != "/" else path
        for virtual, actual in self._virtual_to_real:
            if normalized == virtual:
                return actual
            if normalized.startswith(f"{virtual}/"):
                suffix = normalized[len(virtual) + 1 :]
                return f"{actual}/{suffix}"
        return path

    def _restore_virtual_path(self, path: str) -> str:
        normalized = path.rstrip("/") if path != "/" else path
        for actual, virtual in self._real_to_virtual:
            if normalized == actual:
                return virtual
            if normalized.startswith(f"{actual}/"):
                suffix = normalized[len(actual) + 1 :]
                return f"{virtual}/{suffix}"
        return path

    def _replace_virtual_paths_in_command(self, command: str) -> str:
        transformed = command
        for virtual, actual in sorted(self._virtual_to_real, key=lambda pair: len(pair[0]), reverse=True):
            pattern = re.compile(rf"(?<![:\\w]){re.escape(virtual)}(?=(/|\b))")
            transformed = pattern.sub(actual, transformed)
        return transformed

    def _restore_virtual_paths_in_output(self, output: str) -> str:
        transformed = output
        for actual, virtual in self._real_to_virtual:
            transformed = transformed.replace(actual, virtual)
        return transformed

    def execute_command(self, command: str) -> str:
        self._skill_sync.sync_for_text(command)
        mapped_command = self._replace_virtual_paths_in_command(command)

        with self._lock:
            output, code = self._client.execute_command(mapped_command, exec_dir=self._sandbox_root)

        restored_output = self._restore_virtual_paths_in_output(output)
        if restored_output:
            return restored_output
        if code == 0:
            return "(no output)"
        return f"Error: command failed with exit code {code}"

    def read_file(self, path: str) -> str:
        self._skill_sync.sync_for_text(path)
        remote_path = self._map_virtual_path(path)

        command = f"cat {shlex.quote(remote_path)}"
        output, code = self._client.execute_command(command, exec_dir=self._sandbox_root)
        output = self._restore_virtual_paths_in_output(output)

        if code != 0:
            return f"Error: {output}"
        return output

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        self._skill_sync.sync_for_text(path)
        remote_path = self._map_virtual_path(path)

        command = (
            f"find {shlex.quote(remote_path)} -maxdepth {max_depth} "
            f"\\( -type f -o -type d \\) 2>/dev/null | head -500"
        )
        output, code = self._client.execute_command(command, exec_dir=self._sandbox_root)
        if code != 0 and not output:
            return []

        result: list[str] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            result.append(self._restore_virtual_path(line))
        return result

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        self._skill_sync.sync_for_text(path)
        remote_path = self._map_virtual_path(path)

        write_content = content
        if append:
            existing = self.read_file(path)
            if existing.startswith("Error:"):
                existing = ""
            write_content = f"{existing}{content}"

        if not self._client.write_file_content(remote_path, write_content):
            raise RuntimeError(f"Failed to write file in CMB sandbox: {path}")

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        remote_root = self._map_virtual_path(path)

        type_filter = "\\( -type f -o -type d \\)" if include_dirs else "-type f"
        candidate_limit = max(max_results * 20, max_results + 1)
        command = (
            f"find {shlex.quote(remote_root)} -mindepth 1 {type_filter} 2>/dev/null "
            f"| head -n {candidate_limit}"
        )
        output, _ = self._client.execute_command(command, exec_dir=self._sandbox_root)

        matches: list[str] = []
        for raw_path in output.splitlines():
            candidate = raw_path.strip()
            if not candidate or should_ignore_path(candidate):
                continue

            rel_path = self._relative_path(candidate, remote_root)
            if path_matches(pattern, rel_path):
                matches.append(self._restore_virtual_path(candidate))
                if len(matches) >= max_results:
                    return matches, True

        return matches, False

    def grep(
        self,
        path: str,
        pattern: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> tuple[list[GrepMatch], bool]:
        import re as _re

        regex_source = _re.escape(pattern) if literal else pattern
        _re.compile(regex_source, 0 if case_sensitive else _re.IGNORECASE)

        remote_root = self._map_virtual_path(path)

        command_parts = ["grep", "-R", "-n", "-I"]
        command_parts.append("-F" if literal else "-E")
        if not case_sensitive:
            command_parts.append("-i")
        if glob is not None:
            command_parts.append(f"--include={glob}")
        command_parts.extend(["--", pattern, remote_root])

        quoted_command = " ".join(shlex.quote(part) for part in command_parts)
        command = f"{quoted_command} 2>/dev/null | head -n {max_results + 1}"

        output, code = self._client.execute_command(command, exec_dir=self._sandbox_root)
        if code != 0 and not output.strip():
            return [], False

        matches: list[GrepMatch] = []
        for row in output.splitlines():
            parts = row.split(":", 2)
            if len(parts) != 3:
                continue

            raw_path, raw_line_number, raw_line = parts
            if should_ignore_path(raw_path):
                continue

            if glob is not None:
                rel_path = self._relative_path(raw_path, remote_root)
                if not path_matches(glob, rel_path):
                    continue

            try:
                line_number = int(raw_line_number)
            except ValueError:
                continue

            matches.append(
                GrepMatch(
                    path=self._restore_virtual_path(raw_path),
                    line_number=line_number,
                    line=truncate_line(raw_line),
                )
            )
            if len(matches) >= max_results:
                return matches, True

        return matches, False

    def update_file(self, path: str, content: bytes) -> None:
        remote_path = self._map_virtual_path(path)
        if not self._client.write_file_binary(remote_path, content):
            raise RuntimeError(f"Failed to update file in CMB sandbox: {path}")

    def close(self) -> None:
        self._client.cleanup()
        self._client.close()

    @staticmethod
    def _relative_path(candidate: str, root: str) -> str:
        root_value = root.rstrip("/")
        candidate_value = candidate.rstrip("/")

        if candidate_value == root_value:
            return ""
        if candidate_value.startswith(f"{root_value}/"):
            return candidate_value[len(root_value) + 1 :]

        return PurePosixPath(candidate_value).name
