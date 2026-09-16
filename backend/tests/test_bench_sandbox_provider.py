from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load_module(name: str, relative: str):
    path = Path(__file__).resolve().parents[1] / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load_module("bench_provider", "scripts/benchmark/sandbox/bench_provider.py")
summarize = _load_module("summarize", "scripts/benchmark/sandbox/summarize.py")


class _FakeProvider:
    def __init__(self, sandbox: Any | None = None) -> None:
        self._lock = bench.threading.Lock()
        self._warm_pool: dict[str, tuple[Any, float]] = {}
        self._sandbox = sandbox or _FakeSandbox("ok")
        self.released: list[str] = []
        self.shutdown_called = False

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        return "sandbox-id"

    def get(self, sandbox_id: str):
        return self._sandbox

    def release(self, sandbox_id: str) -> None:
        self.released.append(sandbox_id)

    def shutdown(self) -> None:
        self.shutdown_called = True


class _FakeWarmReclaimProvider(_FakeProvider):
    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        reclaimed = self._reclaim_warm_pool("sandbox-id")
        assert reclaimed is not None
        return reclaimed

    def _reclaim_warm_pool(self, sandbox_id: str) -> str | None:
        return sandbox_id


class _FakeSandbox:
    def __init__(self, output: str | Exception) -> None:
        self.output = output

    def execute_command(self, command: str, timeout: float | None = None) -> str:
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def test_aio_provider_default_leaves_image_unset(monkeypatch, tmp_path):
    captured_config: dict[str, Any] = {}

    def _factory(config: dict[str, Any]):
        captured_config.update(config)
        return _FakeProvider(), {"replicas": config["replicas"], "idle_timeout": config["idle_timeout"], "image": config.get("image")}

    monkeypatch.setitem(bench.PROVIDER_FACTORIES, "aio-docker", _factory)

    rc = bench.main(
        [
            "--provider",
            "aio-docker",
            "--iterations",
            "0",
            "--warmup-iterations",
            "0",
            "--output",
            str(tmp_path / "out.jsonl"),
        ]
    )

    assert rc == 0
    assert captured_config["image"] is None


def test_explicit_aio_provider_image_is_forwarded(monkeypatch, tmp_path):
    captured_config: dict[str, Any] = {}

    def _factory(config: dict[str, Any]):
        captured_config.update(config)
        return _FakeProvider(), {"replicas": config["replicas"], "idle_timeout": config["idle_timeout"], "image": config.get("image")}

    monkeypatch.setitem(bench.PROVIDER_FACTORIES, "aio-docker", _factory)

    rc = bench.main(
        [
            "--provider",
            "aio-docker",
            "--image",
            "custom/aio:latest",
            "--iterations",
            "0",
            "--warmup-iterations",
            "0",
            "--output",
            str(tmp_path / "out.jsonl"),
        ]
    )

    assert rc == 0
    assert captured_config["image"] == "custom/aio:latest"


def test_failed_turn_releases_acquired_sandbox() -> None:
    provider = _FakeProvider(_FakeSandbox(RuntimeError("boom")))

    result = bench._run_one_turn(
        provider=provider,
        provider_name="fake",
        scenario="warm_same_thread",
        workload_name="noop",
        command="true",
        iteration=0,
        concurrency=1,
        user_id="user",
        thread_id="thread",
        no_warmpool=False,
    )

    assert result.success is False
    assert provider.released == ["sandbox-id"]


def test_error_string_output_records_failed_turn() -> None:
    provider = _FakeProvider(_FakeSandbox("Error: vsock disconnected"))

    result = bench._run_one_turn(
        provider=provider,
        provider_name="fake",
        scenario="warm_same_thread",
        workload_name="noop",
        command="true",
        iteration=0,
        concurrency=1,
        user_id="user",
        thread_id="thread",
        no_warmpool=False,
    )

    assert result.success is False
    assert result.error == "Error: vsock disconnected"
    assert provider.released == ["sandbox-id"]


def test_warm_hit_uses_reclaim_instrumentation_not_pre_acquire_sample() -> None:
    provider = _FakeWarmReclaimProvider(_FakeSandbox("ok"))
    bench._install_warm_hit_tracking(provider)

    result = bench._run_one_turn(
        provider=provider,
        provider_name="fake",
        scenario="warm_same_thread",
        workload_name="noop",
        command="true",
        iteration=0,
        concurrency=1,
        user_id="user",
        thread_id="thread",
        no_warmpool=False,
    )

    assert result.success is True
    assert result.warm_hit is True


def test_summary_preserves_all_failure_group() -> None:
    rows = [
        {
            "provider": "aio-docker",
            "scenario": "warm_same_thread",
            "workload": "noop",
            "concurrency": 1,
            "success": False,
            "error": "RuntimeError('boom')",
            "acquire_ms": 0,
            "total_ms": 12.3,
        }
    ]

    summary = summarize._summarize(rows, ["provider", "scenario", "workload", "concurrency"])

    assert summary == [
        {
            "provider": "aio-docker",
            "scenario": "warm_same_thread",
            "workload": "noop",
            "concurrency": 1,
            "count": 1,
            "ok": 0,
            "errors": 1,
            "warm_hit_rate": 0,
            "acquire_p50": 0.0,
            "acquire_p95": 0.0,
            "acquire_p99": 0.0,
            "acquire_mean": 0.0,
            "run_p50": 0.0,
            "run_p95": 0.0,
            "release_p50": 0.0,
            "total_p50": 0.0,
            "total_p95": 0.0,
            "total_p99": 0.0,
            "total_mean": 0.0,
        }
    ]
