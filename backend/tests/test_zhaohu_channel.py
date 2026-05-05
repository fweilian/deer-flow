"""Tests for the Zhaohu outbound channel."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.channels.message_bus import MessageBus, OutboundMessage


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _MockResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class _MockAsyncClient:
    def __init__(
        self,
        *,
        post_responses: list[_MockResponse] | None = None,
        post_calls: list[dict[str, Any]] | None = None,
        **kwargs,
    ):
        self._post_responses = list(post_responses or [])
        self._post_calls = post_calls if post_calls is not None else []
        self.kwargs = kwargs
        self.closed = False

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        data: Any = None,
        files: Any = None,
        headers: dict[str, Any] | None = None,
        **kwargs,
    ) -> _MockResponse:
        self._post_calls.append(
            {
                "url": url,
                "json": json,
                "data": data,
                "files": files,
                "headers": headers or {},
                **kwargs,
            }
        )
        if self._post_responses:
            return self._post_responses.pop(0)
        return _MockResponse({"code": 0})

    async def aclose(self) -> None:
        self.closed = True


class TestZhaohuChannel:
    def test_start_requires_credentials(self):
        from app.channels.zhaohu import ZhaohuChannel

        async def go():
            channel = ZhaohuChannel(bus=MessageBus(), config={"enabled": True})
            await channel.start()
            assert channel.is_running is False

        _run(go())

    def test_send_text_fetches_token_and_uses_chat_id_as_to_id(self, monkeypatch):
        from app.channels.zhaohu import ZhaohuChannel

        async def go():
            post_calls: list[dict[str, Any]] = []
            client = _MockAsyncClient(
                post_calls=post_calls,
                post_responses=[
                    _MockResponse({"access_token": "token-1", "expires_in": 7200}),
                    _MockResponse({"code": 0, "msg": "ok"}),
                ],
            )
            monkeypatch.setattr("app.channels.zhaohu.httpx.AsyncClient", lambda **kwargs: client)

            channel = ZhaohuChannel(
                bus=MessageBus(),
                config={
                    "client_id": "cid",
                    "client_secret": "secret",
                    "from_id": "robot-001",
                },
            )
            await channel.start()
            await channel.send(OutboundMessage(channel_name="zhaohu", chat_id="user-123", thread_id="t1", text="hello zhaohu"))

            assert post_calls[0]["data"]["grant_type"] == "client_credentials"
            assert post_calls[1]["json"] == {
                "fromId": "robot-001",
                "toId": "user-123",
                "content": "hello zhaohu",
            }
            assert post_calls[1]["headers"]["Authorization"] == "Bearer token-1"

            await channel.stop()

        _run(go())

    def test_send_reuses_cached_token_until_expiry(self, monkeypatch):
        from app.channels.zhaohu import ZhaohuChannel

        async def go():
            post_calls: list[dict[str, Any]] = []
            client = _MockAsyncClient(
                post_calls=post_calls,
                post_responses=[
                    _MockResponse({"access_token": "cached-token", "expires_in": 7200}),
                    _MockResponse({"code": 0, "msg": "first"}),
                    _MockResponse({"code": 0, "msg": "second"}),
                ],
            )
            monkeypatch.setattr("app.channels.zhaohu.httpx.AsyncClient", lambda **kwargs: client)

            channel = ZhaohuChannel(
                bus=MessageBus(),
                config={
                    "client_id": "cid",
                    "client_secret": "secret",
                    "from_id": "robot-001",
                },
            )
            await channel.start()
            await channel.send(OutboundMessage(channel_name="zhaohu", chat_id="user-1", thread_id="t1", text="one"))
            await channel.send(OutboundMessage(channel_name="zhaohu", chat_id="user-2", thread_id="t2", text="two"))

            token_calls = [call for call in post_calls if call["data"] is not None]
            message_calls = [call for call in post_calls if call["json"] is not None]
            assert len(token_calls) == 1
            assert len(message_calls) == 2

            await channel.stop()

        _run(go())

    def test_send_refreshes_expired_token(self, monkeypatch):
        from app.channels.zhaohu import ZhaohuChannel

        async def go():
            post_calls: list[dict[str, Any]] = []
            client = _MockAsyncClient(
                post_calls=post_calls,
                post_responses=[
                    _MockResponse({"access_token": "token-1", "expires_in": 1}),
                    _MockResponse({"code": 0, "msg": "first"}),
                    _MockResponse({"access_token": "token-2", "expires_in": 7200}),
                    _MockResponse({"code": 0, "msg": "second"}),
                ],
            )
            monkeypatch.setattr("app.channels.zhaohu.httpx.AsyncClient", lambda **kwargs: client)

            channel = ZhaohuChannel(
                bus=MessageBus(),
                config={
                    "client_id": "cid",
                    "client_secret": "secret",
                    "from_id": "robot-001",
                },
            )
            await channel.start()
            await channel.send(OutboundMessage(channel_name="zhaohu", chat_id="user-1", thread_id="t1", text="one"))
            channel._token_expires_at = time.time() - 1
            await channel.send(OutboundMessage(channel_name="zhaohu", chat_id="user-2", thread_id="t2", text="two"))

            token_calls = [call for call in post_calls if call["data"] is not None]
            assert len(token_calls) == 2
            assert post_calls[3]["headers"]["Authorization"] == "Bearer token-2"

            await channel.stop()

        _run(go())

    def test_send_raises_on_business_error(self, monkeypatch):
        from app.channels.zhaohu import ZhaohuChannel

        async def go():
            client = _MockAsyncClient(
                post_responses=[
                    _MockResponse({"access_token": "token-1", "expires_in": 7200}),
                    _MockResponse({"code": 5001, "msg": "send failed"}),
                ],
            )
            monkeypatch.setattr("app.channels.zhaohu.httpx.AsyncClient", lambda **kwargs: client)

            channel = ZhaohuChannel(
                bus=MessageBus(),
                config={
                    "client_id": "cid",
                    "client_secret": "secret",
                    "from_id": "robot-001",
                },
            )
            await channel.start()

            with pytest.raises(RuntimeError, match="send failed"):
                await channel.send(OutboundMessage(channel_name="zhaohu", chat_id="user-1", thread_id="t1", text="hello"))

            await channel.stop()

        _run(go())
