"""Unit tests for the Store provider."""

import sys
from unittest.mock import MagicMock, patch

import pytest

import deerflow.config.app_config as app_config_module
from deerflow.config.checkpointer_config import load_checkpointer_config_from_dict, set_checkpointer_config
from deerflow.config.database_config import DatabaseConfig
from deerflow.runtime.store import get_store, reset_store


@pytest.fixture(autouse=True)
def reset_state():
    app_config_module._app_config = None
    set_checkpointer_config(None)
    reset_store()
    yield
    app_config_module._app_config = None
    set_checkpointer_config(None)
    reset_store()


def test_returns_in_memory_store_when_not_configured():
    from langgraph.store.memory import InMemoryStore

    with patch("deerflow.runtime.store.provider.get_app_config", side_effect=FileNotFoundError):
        store = get_store()

    assert isinstance(store, InMemoryStore)


def test_database_gaussdb_falls_back_to_in_memory_store():
    from langgraph.store.memory import InMemoryStore

    from deerflow.runtime.store.provider import _sync_store_from_database_cm

    db_config = DatabaseConfig(
        backend="gaussdb",
        gaussdb_url="gaussdb://root:1234@localhost:30100/db",
    )

    with _sync_store_from_database_cm(db_config) as store:
        assert isinstance(store, InMemoryStore)


def test_postgres_store_is_created():
    load_checkpointer_config_from_dict({"type": "postgres", "connection_string": "postgresql://localhost/db"})

    mock_store_instance = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=mock_store_instance)
    mock_cm.__exit__ = MagicMock(return_value=False)

    mock_store_cls = MagicMock()
    mock_store_cls.from_conn_string = MagicMock(return_value=mock_cm)

    mock_module = MagicMock()
    mock_module.PostgresStore = mock_store_cls

    with patch.dict(sys.modules, {"langgraph.store.postgres": mock_module}):
        reset_store()
        store = get_store()

    assert store is mock_store_instance
    mock_store_cls.from_conn_string.assert_called_once_with("postgresql://localhost/db")
    mock_store_instance.setup.assert_called_once()
