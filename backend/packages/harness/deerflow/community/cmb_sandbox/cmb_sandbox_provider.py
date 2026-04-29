from __future__ import annotations

import atexit
import hashlib
import logging
import threading
import time
import uuid

from deerflow.config import get_app_config
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider

from .cmb_sandbox import CmbSandbox

logger = logging.getLogger(__name__)

_IDLE_CHECK_INTERVAL = 60


class CmbSandboxProvider(SandboxProvider):
    """Sandbox provider for the CMB HTTP sandbox backend."""

    uses_thread_data_mounts = False

    def __init__(self):
        self._lock = threading.Lock()
        self._sandboxes: dict[str, CmbSandbox] = {}
        self._thread_sandboxes: dict[str, str] = {}
        self._last_activity: dict[str, float] = {}
        self._shutdown_called = False

        sandbox_cfg = getattr(get_app_config(), "sandbox", None)
        raw_idle_timeout = getattr(sandbox_cfg, "cmb_idle_timeout", 0) if sandbox_cfg else 0
        try:
            self._idle_timeout = max(0, int(raw_idle_timeout or 0))
        except (TypeError, ValueError):
            self._idle_timeout = 0

        self._idle_checker_stop = threading.Event()
        self._idle_checker_thread: threading.Thread | None = None

        if self._idle_timeout > 0:
            self._start_idle_checker()

        atexit.register(self.shutdown)

    @staticmethod
    def _deterministic_sandbox_id(thread_id: str) -> str:
        return hashlib.sha256(thread_id.encode()).hexdigest()[:8]

    def _start_idle_checker(self) -> None:
        self._idle_checker_thread = threading.Thread(
            target=self._idle_checker_loop,
            name="cmb-sandbox-idle-checker",
            daemon=True,
        )
        self._idle_checker_thread.start()

    def _idle_checker_loop(self) -> None:
        while not self._idle_checker_stop.wait(timeout=_IDLE_CHECK_INTERVAL):
            try:
                self._cleanup_idle_sandboxes()
            except Exception as exc:
                logger.error("CMB idle checker failed: %s", exc)

    def _cleanup_idle_sandboxes(self) -> None:
        if self._idle_timeout <= 0:
            return

        now = time.time()
        stale_ids: list[str] = []

        with self._lock:
            for sandbox_id, last_ts in self._last_activity.items():
                if now - last_ts > self._idle_timeout:
                    stale_ids.append(sandbox_id)

        for sandbox_id in stale_ids:
            self._destroy_sandbox(sandbox_id)

    def acquire(self, thread_id: str | None = None) -> str:
        with self._lock:
            if thread_id:
                existing_id = self._thread_sandboxes.get(thread_id)
                if existing_id and existing_id in self._sandboxes:
                    self._last_activity[existing_id] = time.time()
                    return existing_id

            sandbox_id = self._deterministic_sandbox_id(thread_id) if thread_id else str(uuid.uuid4())[:8]

            sandbox = self._sandboxes.get(sandbox_id)
            if sandbox is None:
                sandbox = CmbSandbox(id=sandbox_id, thread_id=thread_id or sandbox_id)
                self._sandboxes[sandbox_id] = sandbox

            if thread_id:
                self._thread_sandboxes[thread_id] = sandbox_id

            self._last_activity[sandbox_id] = time.time()
            return sandbox_id

    def get(self, sandbox_id: str) -> Sandbox | None:
        with self._lock:
            sandbox = self._sandboxes.get(sandbox_id)
            if sandbox is not None:
                self._last_activity[sandbox_id] = time.time()
            return sandbox

    def release(self, sandbox_id: str) -> None:
        # Keep sandboxes warm for reuse across turns; just refresh activity.
        with self._lock:
            if sandbox_id in self._sandboxes:
                self._last_activity[sandbox_id] = time.time()

    def _destroy_sandbox(self, sandbox_id: str) -> None:
        sandbox: CmbSandbox | None = None

        with self._lock:
            sandbox = self._sandboxes.pop(sandbox_id, None)
            self._last_activity.pop(sandbox_id, None)
            thread_ids = [thread_id for thread_id, sid in self._thread_sandboxes.items() if sid == sandbox_id]
            for thread_id in thread_ids:
                del self._thread_sandboxes[thread_id]

        if sandbox is not None:
            try:
                sandbox.close()
            except Exception as exc:
                logger.warning("Failed to close CMB sandbox %s: %s", sandbox_id, exc)

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown_called:
                return
            self._shutdown_called = True
            sandbox_ids = list(self._sandboxes.keys())

        self._idle_checker_stop.set()
        if self._idle_checker_thread is not None and self._idle_checker_thread.is_alive():
            self._idle_checker_thread.join(timeout=5)

        for sandbox_id in sandbox_ids:
            self._destroy_sandbox(sandbox_id)
