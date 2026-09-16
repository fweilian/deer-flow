from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.deps import get_config
from app.gateway.routers import features


def _app_with_config(
    *,
    agents_api_enabled: bool,
    mcp_tasks_available: bool = False,
    subagent_batches_available: bool = False,
    subagent_batch_repo_available: bool | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.mcp_tasks_available = mcp_tasks_available
    app.state.subagent_batches_available = subagent_batches_available
    if subagent_batch_repo_available is None:
        subagent_batch_repo_available = subagent_batches_available
    app.state.subagent_batch_repo = object() if subagent_batch_repo_available else None
    app.include_router(features.router)
    fake_config = SimpleNamespace(
        agents_api=SimpleNamespace(enabled=agents_api_enabled),
        subagent_runtime=SimpleNamespace(max_running=3),
    )
    app.dependency_overrides[get_config] = lambda: fake_config
    return app


def test_features_reports_agents_api_enabled() -> None:
    with TestClient(_app_with_config(agents_api_enabled=True)) as client:
        response = client.get("/api/features")
    assert response.status_code == 200
    assert response.json() == {
        "agents_api": {"enabled": True},
        "mcp_tasks": {"enabled": False},
        "subagent_batches": {
            "enabled": False,
            "repository_available": False,
            "worker_running": False,
            "max_running": 3,
        },
    }


def test_features_reports_agents_api_disabled() -> None:
    with TestClient(_app_with_config(agents_api_enabled=False)) as client:
        response = client.get("/api/features")
    assert response.status_code == 200
    assert response.json() == {
        "agents_api": {"enabled": False},
        "mcp_tasks": {"enabled": False},
        "subagent_batches": {
            "enabled": False,
            "repository_available": False,
            "worker_running": False,
            "max_running": 3,
        },
    }


def test_features_reports_mcp_tasks_startup_capability() -> None:
    with TestClient(_app_with_config(agents_api_enabled=True, mcp_tasks_available=True)) as client:
        response = client.get("/api/features")
    assert response.status_code == 200
    assert response.json()["mcp_tasks"] == {"enabled": True}


def test_features_reports_subagent_batch_startup_capability() -> None:
    with TestClient(
        _app_with_config(
            agents_api_enabled=True,
            subagent_batches_available=True,
        )
    ) as client:
        response = client.get("/api/features")
    assert response.status_code == 200
    assert response.json()["subagent_batches"] == {
        "enabled": True,
        "repository_available": True,
        "worker_running": True,
        "max_running": 3,
    }


def test_features_distinguishes_batch_history_from_worker_availability() -> None:
    with TestClient(
        _app_with_config(
            agents_api_enabled=True,
            subagent_batches_available=False,
            subagent_batch_repo_available=True,
        )
    ) as client:
        response = client.get("/api/features")
    assert response.status_code == 200
    assert response.json()["subagent_batches"] == {
        "enabled": False,
        "repository_available": True,
        "worker_running": False,
        "max_running": 3,
    }
