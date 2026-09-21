"""Regression coverage for MySQL-aware optional dependency detection."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DETECT_SCRIPT_PATH = REPO_ROOT / "scripts" / "detect_uv_extras.py"

spec = importlib.util.spec_from_file_location("deerflow_detect_uv_extras", DETECT_SCRIPT_PATH)
assert spec is not None and spec.loader is not None
detect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(detect)


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "UV_EXTRAS",
        "DEER_FLOW_CONFIG_PATH",
        "DEER_FLOW_STREAM_BRIDGE_REDIS_URL",
        "DEER_FLOW_SANDBOX_OWNERSHIP_REDIS_URL",
        "REDIS_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_parse_env_extras_accepts_safe_names_and_rejects_shell_syntax(capsys):
    assert detect.parse_env_extras("mysql,ollama redis") == ["mysql", "ollama", "redis"]
    assert detect.parse_env_extras("mysql;evil") == []
    assert "ignoring invalid UV_EXTRAS entry" in capsys.readouterr().err


def test_detect_from_config_finds_mysql_database_and_checkpointer_once(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "database:\n  backend: mysql\n  mysql_url: $MYSQL_DATABASE_URL\ncheckpointer:\n  type: mysql\n",
        encoding="utf-8",
    )
    assert detect.detect_from_config(config) == ["mysql"]


def test_detect_from_config_combines_mysql_redis_and_ollama(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "database:\n  backend: mysql\nstream_bridge:\n  type: redis\nmodels:\n  - use: langchain_ollama:ChatOllama\n",
        encoding="utf-8",
    )
    assert detect.detect_from_config(config) == ["mysql", "ollama", "redis"]


def test_detect_from_config_ignores_commented_or_nested_values(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "# database:\n#   backend: mysql\ndatabase:\n  backend: sqlite\n  nested:\n    backend: mysql\nmodels:\n  - use: deerflow.models:Chat\n    nested:\n      use: langchain_ollama:ChatOllama\n",
        encoding="utf-8",
    )
    assert detect.detect_from_config(config) == []


def test_resolve_extras_prefers_explicit_env_and_keeps_required_redis(isolated_cwd, monkeypatch):
    (isolated_cwd / "config.yaml").write_text("database:\n  backend: mysql\n", encoding="utf-8")
    monkeypatch.setenv("UV_EXTRAS", "ollama")
    monkeypatch.setenv("DEER_FLOW_STREAM_BRIDGE_REDIS_URL", "redis://redis:6379/0")
    assert detect.resolve_extras() == ["ollama", "redis"]


def test_resolve_extras_reads_mysql_config_when_no_env_override(isolated_cwd):
    (isolated_cwd / "config.yaml").write_text("database:\n  backend: mysql\n", encoding="utf-8")
    assert detect.resolve_extras() == ["mysql"]


def test_format_flags_emits_one_flag_per_extra():
    assert detect.format_flags(["mysql", "redis"]) == "--extra mysql --extra redis"
