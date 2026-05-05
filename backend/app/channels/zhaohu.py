"""Zhaohu channel implementation."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.channels.base import Channel
from app.channels.message_bus import MessageBus, OutboundMessage

logger = logging.getLogger(__name__)

DEFAULT_AUTH_URL = "https://zh-gateway.paas.cn/auth-service/oauth/token"
DEFAULT_API_BASE_URL = "https://zh-gateway.paas.cn"
DEFAULT_TIMEOUT = 20.0
TOKEN_REFRESH_MARGIN_SECONDS = 60


class ZhaohuChannel(Channel):
    """Outbound-only Zhaohu channel.

    DeerFlow's generic ``chat_id`` field is treated as the target Zhaohu user
    ID and is forwarded to the upstream API as ``toId``.
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="zhaohu", bus=bus, config=config)
        self._client: httpx.AsyncClient | None = None
        self._client_id = str(config.get("client_id") or "").strip()
        self._client_secret = str(config.get("client_secret") or "").strip()
        self._from_id = str(config.get("from_id") or "").strip()
        self._auth_url = str(config.get("auth_url") or DEFAULT_AUTH_URL).strip()
        self._api_base_url = str(config.get("api_base_url") or DEFAULT_API_BASE_URL).rstrip("/")
        self._timeout = float(config.get("timeout") or DEFAULT_TIMEOUT)
        self._access_token = ""
        self._token_expires_at = 0.0

    async def start(self) -> None:
        if self._running:
            return

        if not self._client_id or not self._client_secret or not self._from_id:
            logger.error("Zhaohu channel requires client_id, client_secret, and from_id")
            return

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        self.bus.subscribe_outbound(self._on_outbound)
        self._running = True
        logger.info("Zhaohu channel started")

    async def stop(self) -> None:
        self.bus.unsubscribe_outbound(self._on_outbound)
        client = self._client
        self._client = None
        self._running = False
        if client is not None:
            await client.aclose()
        logger.info("Zhaohu channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        if self._client is None:
            raise RuntimeError("Zhaohu channel is not started")
        if not msg.chat_id:
            raise RuntimeError("Zhaohu outbound message is missing target user ID")

        access_token = await self._get_access_token()
        response = await self._client.post(
            f"{self._api_base_url}/robot-service/single-message/text",
            json={
                "fromId": self._from_id,
                "toId": msg.chat_id,
                "content": msg.text,
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        body = response.json()
        if int(body.get("code", -1)) != 0:
            raise RuntimeError(f"Zhaohu send failed: {body.get('msg') or body}")

        logger.info("[Zhaohu] sent text message to user_id=%s", msg.chat_id)

    async def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        if self._client is None:
            raise RuntimeError("Zhaohu channel is not started")

        response = await self._client.post(
            self._auth_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        response.raise_for_status()
        body = response.json()
        token = str(body.get("access_token") or "").strip()
        if not token:
            raise RuntimeError(f"Zhaohu auth failed: {body}")

        expires_in = int(body.get("expires_in") or 0)
        self._access_token = token
        self._token_expires_at = time.time() + max(expires_in - TOKEN_REFRESH_MARGIN_SECONDS, 0)
        return token
