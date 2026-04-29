from __future__ import annotations

from deerflow.community.cmb_sandbox.cmb_sandbox import CmbSandbox


class _FakeClient:
    def __init__(self, *_, **__):
        self.commands: list[str] = []
        self.binary_writes: list[tuple[str, bytes]] = []
        self.text_writes: list[tuple[str, str]] = []

    def execute_command(self, command: str, exec_dir: str = "/opt/sandbox/file"):
        self.commands.append(command)

        if command.startswith("find ") and "-mindepth" in command:
            return (
                "/opt/sandbox/file/workspace/app.py\n"
                "/opt/sandbox/file/workspace/node_modules/skip.py\n",
                0,
            )

        if command.startswith("grep "):
            return "/opt/sandbox/file/workspace/app.py:2:TODO item\n", 0

        if command.startswith("cat "):
            return "hello\n", 0

        return "/opt/sandbox/file/workspace/out.txt\n", 0

    def write_file_content(self, remote_path: str, content: str) -> bool:
        self.text_writes.append((remote_path, content))
        return True

    def write_file_binary(self, remote_path: str, content: bytes) -> bool:
        self.binary_writes.append((remote_path, content))
        return True

    def cleanup(self):
        return None

    def close(self):
        return None


class _FakeSkillSyncManager:
    calls: list[str] = []

    def __init__(self, *_, **__):
        return None

    def sync_for_text(self, text: str) -> None:
        self.calls.append(text)


def test_execute_command_maps_and_restores_virtual_paths(monkeypatch):
    _FakeSkillSyncManager.calls = []
    monkeypatch.setattr("deerflow.community.cmb_sandbox.cmb_sandbox.CmbSandboxClient", _FakeClient)
    monkeypatch.setattr("deerflow.community.cmb_sandbox.cmb_sandbox.CmbSkillSyncManager", _FakeSkillSyncManager)

    sandbox = CmbSandbox(id="sb-1", thread_id="thread-1")
    output = sandbox.execute_command("ls /mnt/user-data/workspace")

    assert "ls /opt/sandbox/file/workspace" in sandbox._client.commands[-1]
    assert "/mnt/user-data/workspace/out.txt" in output
    assert _FakeSkillSyncManager.calls == ["ls /mnt/user-data/workspace"]


def test_glob_filters_ignored_paths_and_restores_virtual_paths(monkeypatch):
    monkeypatch.setattr("deerflow.community.cmb_sandbox.cmb_sandbox.CmbSandboxClient", _FakeClient)
    monkeypatch.setattr("deerflow.community.cmb_sandbox.cmb_sandbox.CmbSkillSyncManager", _FakeSkillSyncManager)

    sandbox = CmbSandbox(id="sb-2", thread_id="thread-2")
    matches, truncated = sandbox.glob("/mnt/user-data/workspace", "**/*.py")

    assert matches == ["/mnt/user-data/workspace/app.py"]
    assert truncated is False


def test_grep_parses_matches_into_grepmatch(monkeypatch):
    monkeypatch.setattr("deerflow.community.cmb_sandbox.cmb_sandbox.CmbSandboxClient", _FakeClient)
    monkeypatch.setattr("deerflow.community.cmb_sandbox.cmb_sandbox.CmbSkillSyncManager", _FakeSkillSyncManager)

    sandbox = CmbSandbox(id="sb-3", thread_id="thread-3")
    matches, truncated = sandbox.grep("/mnt/user-data/workspace", "TODO", glob="**/*.py")

    assert truncated is False
    assert len(matches) == 1
    assert matches[0].path == "/mnt/user-data/workspace/app.py"
    assert matches[0].line_number == 2
    assert "TODO item" in matches[0].line


def test_update_file_maps_virtual_path(monkeypatch):
    monkeypatch.setattr("deerflow.community.cmb_sandbox.cmb_sandbox.CmbSandboxClient", _FakeClient)
    monkeypatch.setattr("deerflow.community.cmb_sandbox.cmb_sandbox.CmbSkillSyncManager", _FakeSkillSyncManager)

    sandbox = CmbSandbox(id="sb-4", thread_id="thread-4")
    sandbox.update_file("/mnt/user-data/uploads/report.pdf", b"pdf")

    assert sandbox._client.binary_writes == [("/opt/sandbox/file/uploads/report.pdf", b"pdf")]
