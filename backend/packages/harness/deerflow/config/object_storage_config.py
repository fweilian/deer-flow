"""Configuration for the shared S3-compatible object store.

The store is deliberately opt-in while Phase 5 migrations are rolled out.  A
caller that asks for object storage while it is disabled receives an explicit
configuration error; there is no filesystem fallback in this layer.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

from deerflow.config.reload_boundary import format_field_description

_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,62}[a-z0-9]$")


class ObjectStorageConfig(BaseModel):
    """S3 endpoint settings captured when the Gateway starts."""

    enabled: bool = Field(
        default=False,
        description="Enable the shared S3-compatible object store. Callers fail closed when it is disabled.",
    )
    endpoint_url: str | None = Field(
        default=None,
        description="Optional S3-compatible endpoint, for example http://minio:9000. Omit for AWS S3.",
    )
    bucket: str | None = Field(default=None, description="Bucket containing DeerFlow Phase 5 objects.")
    access_key_id: str | None = Field(default=None, description="Optional S3 access key; prefer a $ENV_VAR reference.")
    secret_access_key: str | None = Field(default=None, description="Optional S3 secret key; prefer a $ENV_VAR reference.")
    region: str = Field(default="us-east-1", min_length=1, description="S3 signing region.")
    force_path_style: bool = Field(default=True, description="Use path-style S3 addressing; required by the bundled MinIO setup.")
    verify_tls: bool = Field(default=True, description="Verify TLS certificates when endpoint_url uses HTTPS.")
    key_prefix: str = Field(
        default="deer-flow/v1",
        description=format_field_description(
            "object_storage",
            field_doc="Stable key prefix for Phase 5 objects. Changing it requires a restart and does not move existing objects.",
        ),
    )

    @field_validator("endpoint_url")
    @classmethod
    def _validate_endpoint_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("endpoint_url must use http:// or https://.")
        return normalized

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not _BUCKET_PATTERN.fullmatch(normalized):
            raise ValueError("bucket must be a lowercase S3-compatible bucket name.")
        return normalized

    @field_validator("key_prefix")
    @classmethod
    def _validate_key_prefix(cls, value: str) -> str:
        normalized = value.strip("/")
        if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("key_prefix must contain non-empty, non-traversing path segments.")
        return normalized

    @model_validator(mode="after")
    def _require_bucket_when_enabled(self) -> ObjectStorageConfig:
        if self.enabled and not self.bucket:
            raise ValueError("object_storage.bucket is required when object_storage.enabled is true.")
        return self
