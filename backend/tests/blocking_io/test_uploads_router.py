"""Regression anchor: HTTP upload streaming must stay on the async path."""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from _router_auth_helpers import call_unwrapped
from fastapi import UploadFile

from app.gateway.routers import uploads

pytestmark = pytest.mark.asyncio


async def test_upload_endpoint_streams_to_shared_storage_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[bytes] = []

    class Storage:
        async def write_stream(self, _filename, chunks, *, content_type=None):  # noqa: ARG002
            payloads.append(b"".join([chunk async for chunk in chunks]))

    class Factory:
        @classmethod
        def from_app_config(cls, *_args, **_kwargs):
            return Storage()

    monkeypatch.setattr(uploads, "UploadsStorage", Factory)
    result = await call_unwrapped(
        uploads.upload_files,
        "thread-1",
        request=SimpleNamespace(),
        files=[UploadFile(filename="notes.txt", file=BytesIO(b"hello uploads"))],
        config=SimpleNamespace(),
    )

    assert result.success is True
    assert payloads == [b"hello uploads"]
