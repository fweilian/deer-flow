"""Pin the local MinIO environment required by the Phase 5 storage foundation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATHS = {
    "prod": REPO_ROOT / "docker" / "docker-compose.yaml",
    "dev": REPO_ROOT / "docker" / "docker-compose-dev.yaml",
}


@pytest.mark.parametrize("variant", sorted(COMPOSE_PATHS))
def test_compose_provides_healthy_initialized_minio(variant: str):
    compose = yaml.safe_load(COMPOSE_PATHS[variant].read_text(encoding="utf-8"))
    services = compose["services"]
    minio = services["minio"]
    initializer = services["minio-init"]

    assert minio["volumes"] == ["minio-data:/data"]
    assert minio["healthcheck"]["test"] == ["CMD", "curl", "-f", "http://127.0.0.1:9000/minio/health/live"]
    assert minio["environment"]["MINIO_ROOT_USER"] == "${MINIO_ROOT_USER:-minioadmin}"
    assert minio["environment"]["MINIO_ROOT_PASSWORD"] == "${MINIO_ROOT_PASSWORD:-minioadmin}"
    assert initializer["depends_on"]["minio"]["condition"] == "service_healthy"
    assert "mc mb --ignore-existing" in initializer["command"][2]
    assert "$$DEER_FLOW_OBJECT_STORAGE_BUCKET" in initializer["command"][2]
    assert services["gateway"]["depends_on"]["minio-init"]["condition"] == "service_completed_successfully"
    assert "MINIO_ROOT_USER=${MINIO_ROOT_USER:-minioadmin}" in services["gateway"]["environment"]
    assert "MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD:-minioadmin}" in services["gateway"]["environment"]
    assert "DEER_FLOW_OBJECT_STORAGE_BUCKET=${DEER_FLOW_OBJECT_STORAGE_BUCKET:-deer-flow}" in services["gateway"]["environment"]
    assert "minio-data" in compose["volumes"]


def test_example_config_enables_the_bundled_minio_outputs_store():
    config = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    storage = config["object_storage"]

    assert storage == {
        "enabled": True,
        "endpoint_url": "http://minio:9000",
        "bucket": "$DEER_FLOW_OBJECT_STORAGE_BUCKET",
        "access_key_id": "$MINIO_ROOT_USER",
        "secret_access_key": "$MINIO_ROOT_PASSWORD",
        "region": "us-east-1",
        "force_path_style": True,
        "verify_tls": False,
        "key_prefix": "deer-flow/v1",
    }
