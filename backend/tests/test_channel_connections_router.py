"""Router tests for the generic Channel connection API."""

from __future__ import annotations

from tempfile import TemporaryDirectory
from uuid import UUID

import anyio
from _router_auth_helpers import make_authed_test_app
from fastapi.testclient import TestClient

from app.channels.runtime_config_store import ChannelRuntimeConfigStore
from app.gateway.auth.models import User
from app.gateway.routers import channel_connections
from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.config.channel_connections_config import ChannelConnectionsConfig
from deerflow.persistence.channel_connections import ChannelConnectionRepository
from deerflow.persistence.engine import get_session_factory, init_engine


def _admin_user() -> User:
    return User(
        id=UUID("11111111-2222-3333-4444-555555555555"),
        email="alice@example.com",
        password_hash="x",
        system_role="admin",
    )


def _make_app(config: ChannelConnectionsConfig, repo=None):
    app = make_authed_test_app(user_factory=_admin_user)
    app.state.channel_connections_config = config
    app.state.channel_connection_repo = repo
    tmpdir = TemporaryDirectory()
    app.state.channel_runtime_config_tmpdir = tmpdir
    app.state.channel_runtime_config_store = ChannelRuntimeConfigStore(f"{tmpdir.name}/runtime-config.json")
    app.include_router(channel_connections.router)
    return app


def test_provider_catalog_is_empty_until_an_extension_registers_a_channel():
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    try:
        app = _make_app(ChannelConnectionsConfig(enabled=True, providers={}))
        with TestClient(app) as client:
            response = client.get("/api/channels/providers")
        assert response.status_code == 200
        assert response.json() == {"enabled": True, "providers": []}
    finally:
        reset_app_config()


def test_unknown_provider_is_not_an_implicit_config_attribute():
    set_app_config(AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}))
    try:
        app = _make_app(ChannelConnectionsConfig(enabled=True, providers={}))
        with TestClient(app) as client:
            response = client.get("/api/channels/unknown")
        assert response.status_code == 404
    finally:
        reset_app_config()


async def _make_repo(tmp_path):
    await init_engine("sqlite", url=f"sqlite+aiosqlite:///{tmp_path / 'router.db'}", sqlite_dir=str(tmp_path))
    return ChannelConnectionRepository(get_session_factory())


def test_connection_list_keeps_generic_persistence_api(tmp_path):
    repo = anyio.run(_make_repo, tmp_path)
    app = _make_app(ChannelConnectionsConfig(enabled=True, providers={}), repo)
    with TestClient(app) as client:
        response = client.get("/api/channels/connections")
    assert response.status_code == 200
    assert response.json() == {"connections": []}
    anyio.run(repo.close)
