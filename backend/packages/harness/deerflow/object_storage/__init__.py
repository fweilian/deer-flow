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
from deerflow.object_storage.uploads import UPLOADS_VIRTUAL_PREFIX, UploadObject, UploadsStorage, UploadStorageError

__all__ = [
    "ByteRange",
    "ObjectKeyNamespace",
    "OUTPUTS_VIRTUAL_PREFIX",
    "OutputObject",
    "OutputStorageError",
    "OutputsStorage",
    "UPLOADS_VIRTUAL_PREFIX",
    "UploadObject",
    "UploadStorageError",
    "UploadsStorage",
    "ObjectMetadata",
    "ObjectRead",
    "ObjectStorage",
    "ObjectStorageConfigurationError",
    "S3ObjectStorage",
    "get_object_storage",
]
