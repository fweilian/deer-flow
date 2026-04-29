from __future__ import annotations

import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx

from deerflow.config import get_app_config

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://cmb-agent-sandbox.local.cn/agent/sandbox/api/v1"
_DEFAULT_SOURCE = "deerflow-cmb-sandbox"
_DEFAULT_SANDBOX_TIMEOUT = 120
_DEFAULT_REQUEST_TIMEOUT = 120.0
_DEFAULT_LOG_TRUNCATE_LENGTH = 500
_SUCCESS_RETURN_CODE = "SUC0000"
_INSTANCE_EXPIRED_KEYWORDS = (
    "instance expired",
    "instance not found",
    "invalid instance",
    "sandbox instance not found",
    "sandbox not found",
    "实例已过期",
    "实例不存在",
    "实例已销毁",
    "沙箱实例已过期",
    "沙箱实例不存在",
    "沙箱实例已销毁",
    "已过期",
    "已销毁",
)
_SESSION_EXPIRED_KEYWORDS = (
    "session not found",
    "invalid session",
    "session expired",
    "shell session not found",
    "会话不存在",
    "会话已过期",
    "shell会话不存在",
)


@dataclass(frozen=True)
class CmbSandboxConnectionConfig:
    base_url: str
    api_key: str
    source: str
    sandbox_timeout: int
    request_timeout: float
    log_truncate_length: int


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _resolve_sandbox_config() -> CmbSandboxConnectionConfig:
    cfg = get_app_config()
    sandbox_cfg = getattr(cfg, "sandbox", None)

    user_url = _coalesce(
        os.environ.get("SANDBOX_BASE_URL"),
        os.environ.get("SANDBOX_API_BASE_URL"),
    )
    user_key = _coalesce(
        os.environ.get("SANDBOX_API_KEY"),
        os.environ.get("SANDBOX_KEY"),
    )

    cfg_url = _coalesce(
        getattr(sandbox_cfg, "base_url", None),
        getattr(sandbox_cfg, "cmb_base_url", None),
    )
    cfg_key = _coalesce(
        getattr(sandbox_cfg, "api_key", None),
        getattr(sandbox_cfg, "cmb_api_key", None),
    )

    toolkit_host = os.environ.get("TOOLKIT_HOST", "")
    uat_keyword = str(getattr(sandbox_cfg, "cmb_uat_host_keyword", "") or "")
    prd_keyword = str(getattr(sandbox_cfg, "cmb_prd_host_keyword", "") or "")

    uat_url = _coalesce(getattr(sandbox_cfg, "cmb_uat_base_url", None), cfg_url, _DEFAULT_BASE_URL)
    prd_url = _coalesce(getattr(sandbox_cfg, "cmb_prd_base_url", None), cfg_url, _DEFAULT_BASE_URL)
    uat_key = _coalesce(getattr(sandbox_cfg, "cmb_uat_api_key", None), cfg_key, "")
    prd_key = _coalesce(getattr(sandbox_cfg, "cmb_prd_api_key", None), cfg_key, "")

    derived_url = uat_url
    derived_key = uat_key
    if prd_keyword and prd_keyword in toolkit_host:
        derived_url = prd_url
        derived_key = prd_key
    elif uat_keyword and uat_keyword in toolkit_host:
        derived_url = uat_url
        derived_key = uat_key

    final_url = str(_coalesce(user_url, derived_url, cfg_url, _DEFAULT_BASE_URL)).rstrip("/")
    final_key = str(_coalesce(user_key, derived_key, cfg_key, ""))
    source = str(_coalesce(getattr(sandbox_cfg, "cmb_source", None), _DEFAULT_SOURCE))

    sandbox_timeout = int(_coalesce(getattr(sandbox_cfg, "cmb_timeout", None), _DEFAULT_SANDBOX_TIMEOUT))
    request_timeout = float(_coalesce(getattr(sandbox_cfg, "cmb_request_timeout", None), _DEFAULT_REQUEST_TIMEOUT))
    log_truncate_length = int(
        _coalesce(getattr(sandbox_cfg, "cmb_log_truncate_length", None), _DEFAULT_LOG_TRUNCATE_LENGTH)
    )

    return CmbSandboxConnectionConfig(
        base_url=final_url,
        api_key=final_key,
        source=source,
        sandbox_timeout=sandbox_timeout,
        request_timeout=request_timeout,
        log_truncate_length=log_truncate_length,
    )


class CmbSandboxClient:
    """HTTP API client for CMB sandbox endpoints."""

    def __init__(
        self,
        thread_id: str,
        sandbox_timeout: int | None = None,
        log_truncate_length: int | None = None,
        *,
        config: CmbSandboxConnectionConfig | None = None,
        client: httpx.Client | None = None,
    ):
        self.thread_id = thread_id
        self.instance_name: str | None = None
        self.instance_host: str | None = None
        self.file_base_url: str | None = None
        self.shell_session_id: str | None = None

        self._config = config or _resolve_sandbox_config()
        self.sandbox_timeout = sandbox_timeout if sandbox_timeout is not None else self._config.sandbox_timeout
        self.log_truncate_length = (
            log_truncate_length if log_truncate_length is not None else self._config.log_truncate_length
        )

        self._lock = threading.Lock()
        self._exec_lock = threading.Lock()
        self._client = client or httpx.Client(timeout=self._config.request_timeout)
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        headers = {"X-SOURCE": self._config.source}
        if self._config.api_key:
            headers["X-API-KEY"] = self._config.api_key
        return headers

    @staticmethod
    def _contains_any_keyword(message: str, keywords: tuple[str, ...]) -> bool:
        normalized = (message or "").strip().lower()
        if not normalized:
            return False
        return any(keyword in normalized for keyword in keywords)

    def _is_instance_expired_error(self, message: str) -> bool:
        return self._contains_any_keyword(message, _INSTANCE_EXPIRED_KEYWORDS)

    def _is_session_expired_error(self, message: str) -> bool:
        return self._contains_any_keyword(message, _SESSION_EXPIRED_KEYWORDS)

    def _reset_shell_session(self) -> None:
        with self._lock:
            self.shell_session_id = None

    def _reset_instance(self) -> None:
        with self._lock:
            self.shell_session_id = None
            self.instance_name = None
            self.instance_host = None
            self.file_base_url = None

    def create_instance(self) -> bool:
        """Create a sandbox instance and cache its host fields."""
        url = f"{self._config.base_url}/instance"
        payload = {"timeout": self.sandbox_timeout}

        try:
            response = self._client.post(
                url=url,
                headers=self._headers(),
                json=payload,
                timeout=30,
                follow_redirects=True,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.error("[%s] Failed to create CMB sandbox instance: %s", self.thread_id, exc, exc_info=True)
            return False

        if str(data.get("returnCode")) != _SUCCESS_RETURN_CODE:
            logger.error("[%s] CMB sandbox create returned error payload: %s", self.thread_id, data)
            return False

        body = data.get("body") or {}
        instance_host = _coalesce(body.get("alb_host"), body.get("instance_host"), body.get("host"))
        if not instance_host:
            logger.error("[%s] CMB sandbox create response missing host: %s", self.thread_id, data)
            return False

        self.instance_name = str(
            _coalesce(body.get("instance_name"), body.get("instance_id"), body.get("instanceId"), "")
        ) or None
        self.instance_host = str(instance_host)
        if self.instance_host.startswith(("http://", "https://")):
            self.file_base_url = self.instance_host.rstrip("/")
        else:
            self.file_base_url = f"http://{self.instance_host}"

        logger.debug("[%s] CMB sandbox instance ready at %s", self.thread_id, self.file_base_url)
        return True

    def ensure_active(self) -> None:
        """Ensure instance exists before issuing shell/file requests."""
        if self.instance_host:
            return

        with self._lock:
            if self.instance_host:
                return
            if not self.create_instance():
                raise RuntimeError("unable to initialize CMB sandbox instance")

    def upload_file(self, local_file_path: str, target_path: str, *, filename_override: str | None = None) -> bool:
        """Upload a local file to sandbox target directory."""
        file_path = Path(local_file_path)
        filename = filename_override or file_path.name
        content = file_path.read_bytes()

        for attempt in range(2):
            self.ensure_active()
            if self.file_base_url is None:
                return False

            url = f"{self.file_base_url}/v1/file/upload"

            try:
                response = self._client.post(
                    url=url,
                    files={"file": (filename, content)},
                    data={"path": target_path},
                    timeout=60,
                    follow_redirects=True,
                )
                response.raise_for_status()
            except Exception as exc:
                if attempt == 0:
                    logger.info("[%s] Upload failed; resetting sandbox instance and retrying once: %s", self.thread_id, exc)
                    self._reset_instance()
                    continue
                logger.error("[%s] CMB sandbox upload failed: %s", self.thread_id, exc, exc_info=True)
                return False

            # Some deployments return raw 200 without JSON payload.
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower():
                return True

            try:
                data = response.json()
            except Exception:
                return True

            if "returnCode" not in data:
                return True

            if str(data.get("returnCode")) == _SUCCESS_RETURN_CODE:
                return True

            error_msg = str(data.get("errorMsg") or data.get("message") or "")
            if attempt == 0 and self._is_instance_expired_error(error_msg):
                logger.info(
                    "[%s] Upload returned expired sandbox instance; recreating and retrying once: %s",
                    self.thread_id,
                    error_msg,
                )
                self._reset_instance()
                continue
            return False

        return False

    def create_shell_session(self, exec_dir: str = "/opt/sandbox/file") -> bool:
        """Create or reuse a shell session bound to the current thread."""
        self.ensure_active()

        if self.shell_session_id:
            return True

        with self._lock:
            if self.shell_session_id:
                return True
            if self.file_base_url is None:
                return False

            payload = {
                "session_id": self.thread_id,
                "exec_dir": exec_dir,
            }

            try:
                response = self._client.post(
                    url=f"{self.file_base_url}/v1/shell/sessions/create",
                    json=payload,
                    timeout=15,
                    follow_redirects=True,
                )
                response.raise_for_status()
                data = response.json()
            except Exception as exc:
                logger.error("[%s] CMB shell session creation failed: %s", self.thread_id, exc, exc_info=True)
                return False

            if str(data.get("returnCode")) != _SUCCESS_RETURN_CODE:
                error_msg = str(data.get("errorMsg") or data.get("message") or "")
                if self._is_instance_expired_error(error_msg):
                    self._reset_instance()
                logger.error("[%s] CMB shell session create returned error payload: %s", self.thread_id, data)
                return False

            self.shell_session_id = self.thread_id
            return True

    def execute_command(self, command: str, exec_dir: str = "/opt/sandbox/file") -> tuple[str, int]:
        """Execute shell command through CMB API."""
        for attempt in range(2):
            self.ensure_active()
            if not self.create_shell_session(exec_dir):
                if attempt == 0:
                    self._reset_instance()
                    continue
                return "Sandbox session not available", -1
            if self.file_base_url is None:
                if attempt == 0:
                    self._reset_instance()
                    continue
                return "Sandbox file host not available", -1

            payload = {
                "session_id": self.shell_session_id,
                "exec_dir": exec_dir,
                "command": command,
                "async_mode": True,
            }

            with self._exec_lock:
                try:
                    response = self._client.post(
                        url=f"{self.file_base_url}/v1/shell/exec",
                        json=payload,
                        timeout=max(120, float(self.sandbox_timeout)),
                        follow_redirects=True,
                    )
                    response.raise_for_status()
                    data = response.json()
                except Exception as exc:
                    if attempt == 0:
                        logger.info(
                            "[%s] Exec request failed; resetting sandbox instance and retrying once: %s",
                            self.thread_id,
                            exc,
                        )
                        self._reset_instance()
                        continue
                    return f"Execution Exception: {exc}", -1

            if str(data.get("returnCode")) != _SUCCESS_RETURN_CODE:
                error_msg = str(data.get("errorMsg") or data.get("message") or "unknown error")
                if attempt == 0 and self._is_instance_expired_error(error_msg):
                    logger.info(
                        "[%s] Sandbox instance expired/destroyed; recreating and retrying once: %s",
                        self.thread_id,
                        error_msg,
                    )
                    self._reset_instance()
                    continue
                if attempt == 0 and self._is_session_expired_error(error_msg):
                    logger.info("[%s] Shell session expired; recreating session and retrying once: %s", self.thread_id, error_msg)
                    self._reset_shell_session()
                    continue
                return f"Sandbox Error: {error_msg}", -1

            body = data.get("body") or {}
            code = int(body.get("code", 0) or 0)
            output = str(body.get("output", "") or "")

            if output:
                logger.debug(
                    "[%s] Exec code=%s output=%s",
                    self.thread_id,
                    code,
                    output[: self.log_truncate_length],
                )

            return output, code

        return "Sandbox Error: command execution retry exhausted", -1

    def write_file_content(self, remote_path: str, content: str) -> bool:
        """Write UTF-8 content to remote path using upload endpoint."""
        return self.write_file_binary(remote_path, content.encode("utf-8"))

    def write_file_binary(self, remote_path: str, content: bytes) -> bool:
        """Write binary content to remote path using upload endpoint."""
        self.ensure_active()

        target_dir = os.path.dirname(remote_path) or "."
        final_filename = os.path.basename(remote_path)

        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_file.write(content)
            tmp_path = tmp_file.name

        try:
            return self.upload_file(tmp_path, target_dir, filename_override=final_filename)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def get_download_url(self, remote_path: str) -> str:
        self.ensure_active()
        if self.file_base_url is None:
            raise RuntimeError("sandbox file host not available")
        return f"{self.file_base_url}/v1/file/download?path={quote_plus(remote_path)}"

    def cleanup(self) -> None:
        """Delete shell session and instance if API returns identifiers."""
        if self.file_base_url and self.shell_session_id:
            try:
                response = self._client.delete(
                    url=f"{self.file_base_url}/v1/shell/sessions/{self.shell_session_id}",
                    follow_redirects=True,
                )
                response.raise_for_status()
            except Exception as exc:
                logger.warning("[%s] Failed to delete CMB shell session: %s", self.thread_id, exc)

        if self.instance_name:
            try:
                response = self._client.delete(
                    url=f"{self._config.base_url}/instance/{self.instance_name}",
                    headers=self._headers(),
                    follow_redirects=True,
                )
                response.raise_for_status()
            except Exception as exc:
                logger.warning("[%s] Failed to delete CMB sandbox instance: %s", self.thread_id, exc)

        self.shell_session_id = None
        self.instance_name = None
        self.instance_host = None
        self.file_base_url = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
