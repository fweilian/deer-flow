"""Tests for store providers across unified database config modes."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestAsyncStoreProvider:
    @pytest.mark.anyio
    async def test_database_postgres_manual_skips_setup_and_validates_schema(self):
        from deerflow.config.database_config import DatabaseConfig
        from deerflow.runtime.store.async_provider import make_store

        mock_config = MagicMock()
        mock_config.checkpointer = None
        mock_config.database = DatabaseConfig(
            backend="postgres",
            postgres_url="postgresql://localhost/db",
            schema_init="manual",
        )

        mock_store = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__.return_value = mock_store
        mock_cm.__aexit__.return_value = False

        mock_store_cls = MagicMock()
        mock_store_cls.from_conn_string.return_value = mock_cm
        mock_module = MagicMock()
        mock_module.AsyncPostgresStore = mock_store_cls

        with (
            patch("deerflow.runtime.store.async_provider.get_app_config", return_value=mock_config),
            patch.dict(sys.modules, {"langgraph.store.postgres.aio": mock_module}),
            patch(
                "deerflow.runtime.store.async_provider._validate_store_schema",
                new_callable=AsyncMock,
            ) as mock_validate,
        ):
            async with make_store() as store:
                assert store is mock_store

        mock_store_cls.from_conn_string.assert_called_once_with("postgresql://localhost/db")
        mock_store.setup.assert_not_awaited()
        mock_validate.assert_awaited_once_with(mock_store.conn, minimum_version=3)


class TestSyncStoreProvider:
    def test_database_postgres_manual_skips_setup_and_validates_schema(self):
        from deerflow.config.database_config import DatabaseConfig
        from deerflow.runtime.store.provider import store_context

        mock_config = MagicMock()
        mock_config.checkpointer = None
        mock_config.database = DatabaseConfig(
            backend="postgres",
            postgres_url="postgresql://localhost/db",
            schema_init="manual",
        )

        mock_store_instance = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_store_instance)
        mock_cm.__exit__ = MagicMock(return_value=False)

        mock_store_cls = MagicMock()
        mock_store_cls.from_conn_string = MagicMock(return_value=mock_cm)
        mock_module = MagicMock()
        mock_module.PostgresStore = mock_store_cls

        with (
            patch("deerflow.runtime.store.provider.get_app_config", return_value=mock_config),
            patch.dict(sys.modules, {"langgraph.store.postgres": mock_module}),
            patch("deerflow.runtime.store.provider._validate_store_schema") as mock_validate,
        ):
            with store_context() as store:
                assert store is mock_store_instance

        mock_store_instance.setup.assert_not_called()
        mock_validate.assert_called_once_with(mock_store_instance.conn, minimum_version=3)
