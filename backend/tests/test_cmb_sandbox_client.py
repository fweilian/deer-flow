from __future__ import annotations

from types import SimpleNamespace

from deerflow.community.cmb_sandbox.client import (
    CmbSandboxClient,
    CmbSandboxConnectionConfig,
    _resolve_sandbox_config,
)


class _FakeResponse:
    def __init__(self, payload, *, headers=None):
        self._payload = payload
        self.headers = headers or {"content-type": "application/json"}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttpClient:
    def __init__(self):
        self.posts = []
        self.deletes = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/instance"):
            return _FakeResponse(
                {
                    "returnCode": "SUC0000",
                    "body": {
                        "alb_host": "sandbox.internal:18080",
                        "instance_name": "ins-1",
                    },
                }
            )
        if url.endswith("/v1/shell/sessions/create"):
            return _FakeResponse({"returnCode": "SUC0000", "body": {}})
        if url.endswith("/v1/shell/exec"):
            return _FakeResponse({"returnCode": "SUC0000", "body": {"code": 7, "output": "boom"}})
        if url.endswith("/v1/file/upload"):
            return _FakeResponse({"returnCode": "SUC0000"})
        raise AssertionError(f"Unexpected POST url: {url}")

    def delete(self, url, **kwargs):
        self.deletes.append((url, kwargs))
        return _FakeResponse({"returnCode": "SUC0000"})

    def close(self):
        return None


class _FakeHttpClientWithExpiredInstance:
    def __init__(self):
        self.posts = []
        self.instance_create_count = 0
        self.exec_count = 0

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/instance"):
            self.instance_create_count += 1
            host = f"sandbox.internal:{18080 + self.instance_create_count}"
            return _FakeResponse(
                {
                    "returnCode": "SUC0000",
                    "body": {
                        "alb_host": host,
                        "instance_name": f"ins-{self.instance_create_count}",
                    },
                }
            )
        if url.endswith("/v1/shell/sessions/create"):
            return _FakeResponse({"returnCode": "SUC0000", "body": {}})
        if url.endswith("/v1/shell/exec"):
            self.exec_count += 1
            if self.exec_count == 1:
                return _FakeResponse({"returnCode": "FAIL1001", "errorMsg": "沙箱实例已过期或销毁"})
            return _FakeResponse({"returnCode": "SUC0000", "body": {"code": 0, "output": "recovered"}})
        raise AssertionError(f"Unexpected POST url: {url}")

    def close(self):
        return None


def test_resolve_sandbox_config_prefers_environment(monkeypatch):
    cfg = SimpleNamespace(
        sandbox=SimpleNamespace(
            base_url="https://cfg-base.example/api",
            api_key="cfg-key",
            cmb_source="cfg-source",
            cmb_timeout=99,
            cmb_request_timeout=88,
            cmb_log_truncate_length=77,
            cmb_uat_host_keyword="uat",
            cmb_prd_host_keyword="prod",
        )
    )

    monkeypatch.setattr("deerflow.community.cmb_sandbox.client.get_app_config", lambda: cfg)
    monkeypatch.setenv("SANDBOX_BASE_URL", "https://env-base.example/api")
    monkeypatch.setenv("SANDBOX_API_KEY", "env-key")

    resolved = _resolve_sandbox_config()

    assert resolved.base_url == "https://env-base.example/api"
    assert resolved.api_key == "env-key"
    assert resolved.source == "cfg-source"
    assert resolved.sandbox_timeout == 99
    assert resolved.request_timeout == 88.0
    assert resolved.log_truncate_length == 77


def test_execute_command_creates_instance_and_shell_session():
    fake_http = _FakeHttpClient()
    config = CmbSandboxConnectionConfig(
        base_url="https://cmb.example/api/v1",
        api_key="k",
        source="src",
        sandbox_timeout=120,
        request_timeout=30,
        log_truncate_length=100,
    )
    client = CmbSandboxClient(thread_id="thread-1", config=config, client=fake_http)

    output, code = client.execute_command("echo hello")

    assert output == "boom"
    assert code == 7
    assert client.file_base_url == "http://sandbox.internal:18080"
    assert client.shell_session_id == "thread-1"


def test_write_file_binary_uses_remote_filename(monkeypatch):
    fake_http = _FakeHttpClient()
    config = CmbSandboxConnectionConfig(
        base_url="https://cmb.example/api/v1",
        api_key="k",
        source="src",
        sandbox_timeout=120,
        request_timeout=30,
        log_truncate_length=100,
    )
    client = CmbSandboxClient(thread_id="thread-2", config=config, client=fake_http)

    captured = {}

    def _fake_upload(local_file_path, target_path, *, filename_override=None):
        captured["local_file_path"] = local_file_path
        captured["target_path"] = target_path
        captured["filename_override"] = filename_override
        return True

    monkeypatch.setattr(client, "upload_file", _fake_upload)

    ok = client.write_file_binary("/opt/sandbox/file/uploads/report.pdf", b"pdf-bytes")

    assert ok is True
    assert captured["target_path"] == "/opt/sandbox/file/uploads"
    assert captured["filename_override"] == "report.pdf"


def test_execute_command_recreates_expired_instance_and_retries():
    fake_http = _FakeHttpClientWithExpiredInstance()
    config = CmbSandboxConnectionConfig(
        base_url="https://cmb.example/api/v1",
        api_key="k",
        source="src",
        sandbox_timeout=120,
        request_timeout=30,
        log_truncate_length=100,
    )
    client = CmbSandboxClient(thread_id="thread-3", config=config, client=fake_http)

    output, code = client.execute_command("echo retry")

    assert output == "recovered"
    assert code == 0
    assert fake_http.instance_create_count == 2
