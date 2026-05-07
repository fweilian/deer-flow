"""Unit tests for the Store provider."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import deerflow.config.app_config as app_config_module
from deerflow.config.checkpointer_config import load_checkpointer_config_from_dict, set_checkpointer_config
from deerflow.runtime.store import get_store, make_store, reset_store


@pytest.fixture(autouse=True)
def reset_state():
    app_config_module._app_config = None
    set_checkpointer_config(None)
    reset_store()
    yield
    app_config_module._app_config = None
    set_checkpointer_config(None)
    reset_store()


def test_gaussdb_store_requires_compat_package():
    load_checkpointer_config_from_dict({"type": "gaussdb", "connection_string": "gaussdb://localhost/db"})
    with patch.dict(sys.modules, {"deerflow.runtime.store.gaussdb": None}):
        reset_store()
        with pytest.raises(ImportError, match="gaussdb"):
            get_store()


def test_gaussdb_store_requires_connection_string():
    load_checkpointer_config_from_dict({"type": "gaussdb"})
    mock_module = MagicMock()
    mock_module.GaussDBStore = MagicMock()
    with patch.dict(sys.modules, {"deerflow.runtime.store.gaussdb": mock_module}):
        reset_store()
        with pytest.raises(ValueError, match="connection_string is required"):
            get_store()


def test_gaussdb_store_is_created():
    load_checkpointer_config_from_dict({"type": "gaussdb", "connection_string": "gaussdb://localhost/db"})

    mock_store_instance = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=mock_store_instance)
    mock_cm.__exit__ = MagicMock(return_value=False)

    mock_store_cls = MagicMock()
    mock_store_cls.from_conn_string = MagicMock(return_value=mock_cm)

    mock_module = MagicMock()
    mock_module.GaussDBStore = mock_store_cls

    with patch.dict(sys.modules, {"deerflow.runtime.store.gaussdb": mock_module}):
        reset_store()
        store = get_store()

    assert store is mock_store_instance
    mock_store_cls.from_conn_string.assert_called_once_with("gaussdb://localhost/db")
    mock_store_instance.setup.assert_called_once()


@pytest.mark.anyio
async def test_async_gaussdb_store_is_created():
    mock_config = MagicMock()
    mock_config.checkpointer = MagicMock(type="gaussdb", connection_string="gaussdb://localhost/db")

    mock_store = AsyncMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_store
    mock_cm.__aexit__.return_value = False

    mock_store_cls = MagicMock()
    mock_store_cls.from_conn_string.return_value = mock_cm

    mock_module = MagicMock()
    mock_module.AsyncGaussDBStore = mock_store_cls

    with (
        patch("deerflow.runtime.store.async_provider.get_app_config", return_value=mock_config),
        patch.dict(sys.modules, {"deerflow.runtime.store.gaussdb": mock_module}),
    ):
        async with make_store() as store:
            assert store is mock_store

    mock_store_cls.from_conn_string.assert_called_once_with("gaussdb://localhost/db")
    mock_store.setup.assert_awaited_once()
