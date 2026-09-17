"""Narrow shared object-storage contract used by Phase 5 migrations."""

from deerflow.object_storage.keys import ObjectKeyNamespace
from deerflow.object_storage.outputs import OUTPUTS_VIRTUAL_PREFIX, OutputObject, OutputsStorage, OutputStorageError
from deerflow.object_storage.port import (
    ByteRange,
    ObjectMetadata,
    ObjectRead,
    ObjectStorage,
    ObjectStorageConfigurationError,
    S3ObjectStorage,
    get_object_storage,
)

__all__ = [
    "ByteRange",
    "ObjectKeyNamespace",
    "OUTPUTS_VIRTUAL_PREFIX",
    "OutputObject",
    "OutputStorageError",
    "OutputsStorage",
    "ObjectMetadata",
    "ObjectRead",
    "ObjectStorage",
    "ObjectStorageConfigurationError",
    "S3ObjectStorage",
    "get_object_storage",
]
