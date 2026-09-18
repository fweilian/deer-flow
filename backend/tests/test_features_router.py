"""Feature discovery exposes only supported runtime capabilities."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import features


def test_features_reports_agents_api_only() -> None:
    app = FastAPI()
    app.include_router(features.router)
    app.state.app_config = SimpleNamespace(agents_api=SimpleNamespace(enabled=True))

    with TestClient(app) as client:
        response = client.get("/api/features")

    assert response.status_code == 200
    assert response.json() == {"agents_api": {"enabled": True}}
