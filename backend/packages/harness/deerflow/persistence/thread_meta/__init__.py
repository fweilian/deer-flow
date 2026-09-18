"""Thread metadata persistence — ORM, abstract store, and concrete implementations."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.thread_meta.base import PROJECT_FILTER_UNSET, THREAD_ARCHIVED_METADATA_KEY, THREAD_PINNED_METADATA_KEY, THREAD_PROJECT_METADATA_KEY, InvalidMetadataFilterError, ThreadMetaStore, ThreadOwnershipConflictError
from deerflow.persistence.thread_meta.memory import MemoryThreadMetaStore
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.persistence.thread_meta.sql import ThreadMetaRepository

__all__ = [
    "InvalidMetadataFilterError",
    "MemoryThreadMetaStore",
    "PROJECT_FILTER_UNSET",
    "THREAD_PINNED_METADATA_KEY",
    "THREAD_ARCHIVED_METADATA_KEY",
    "THREAD_PINNED_METADATA_KEY",
    "THREAD_PROJECT_METADATA_KEY",
    "ThreadMetaRepository",
    "ThreadMetaRow",
    "ThreadMetaStore",
    "ThreadOwnershipConflictError",
    "make_thread_store",
]


def make_thread_store(
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> ThreadMetaStore:
    """Create the appropriate ThreadMetaStore based on available backends.

    Returns a SQL-backed repository when a session factory is available,
    otherwise falls back to a process-local dictionary implementation.
    """
    if session_factory is not None:
        return ThreadMetaRepository(session_factory)
    return MemoryThreadMetaStore()
