"""Webhook channel for GitHub/Gitee events."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, quote_plus, unquote_plus

from app.channels.base import Channel
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage

logger = logging.getLogger(__name__)

_INSECURE_NO_AUTH = "INSECURE_NO_AUTH"
_DEFAULT_MAX_BODY_BYTES = 1_048_576
_DEFAULT_RATE_LIMIT_PER_MINUTE = 30
_DEFAULT_IDEMPOTENCY_TTL_SECONDS = 3600


class WebhookChannel(Channel):
    """Webhook receiver channel integrated with DeerFlow channel abstractions.

    Config keys (under ``channels.webhook``):
        - ``enabled``: whether to enable this channel.
        - ``max_body_bytes``: max webhook body bytes.
        - ``rate_limit_per_minute``: per-route fixed-window limit.
        - ``idempotency_ttl_seconds``: duplicate delivery cache TTL.
        - ``routes``: mapping of route_name -> route config.

    Route config keys:
        - ``secret``: webhook secret (required unless INSECURE_NO_AUTH for local testing).
        - ``events``: optional allowed event names.
        - ``prompt``: optional prompt template.
        - ``deliver``: default "log"; set to target channel name for forwarding.
        - ``deliver_extra.chat_id``: target user/chat id for forwarding.
        - ``deliver_extra.thread_ts``: optional target thread id.
        - ``deliver_only``: when true, skip LLM and deliver rendered prompt directly.
        - ``gitee_signature_mode``: ``auto`` (default), ``plain``, or ``hmac``.
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="webhook", bus=bus, config=config)
        self._routes: dict[str, dict[str, Any]] = {}
        self._max_body_bytes = int(config.get("max_body_bytes") or _DEFAULT_MAX_BODY_BYTES)
        self._rate_limit_per_minute = int(config.get("rate_limit_per_minute") or _DEFAULT_RATE_LIMIT_PER_MINUTE)
        self._idempotency_ttl_seconds = int(config.get("idempotency_ttl_seconds") or _DEFAULT_IDEMPOTENCY_TTL_SECONDS)
        self._seen_deliveries: dict[str, float] = {}
        self._rate_windows: dict[str, list[float]] = {}

    async def start(self) -> None:
        if self._running:
            return

        self._routes = self._load_routes()
        self._validate_routes(self._routes)

        self.bus.subscribe_outbound(self._on_outbound)
        self._running = True
        logger.info("Webhook channel started with routes: %s", sorted(self._routes.keys()))

    async def stop(self) -> None:
        self.bus.unsubscribe_outbound(self._on_outbound)
        self._running = False
        logger.info("Webhook channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        logger.info(
            "[Webhook] response chat_id=%s thread_id=%s text=%s",
            msg.chat_id,
            msg.thread_id,
            (msg.text or "")[:500],
        )

    async def handle_webhook_request(
        self,
        route_name: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        content_length: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Process one webhook request and return (status_code, payload)."""
        if not self._running:
            return 503, {"error": "Webhook channel is not running"}

        route = self._routes.get(route_name)
        if not route:
            return 404, {"error": f"Unknown route: {route_name}"}

        if content_length is not None and content_length > self._max_body_bytes:
            return 413, {"error": "Payload too large"}
        if len(body) > self._max_body_bytes:
            return 413, {"error": "Payload too large"}

        secret = str(route.get("secret") or "").strip()
        if not secret:
            return 500, {"error": f"Route {route_name} has no secret configured"}

        normalized_headers = {k.lower(): v for k, v in headers.items()}

        if secret != _INSECURE_NO_AUTH and not self._validate_signature(normalized_headers, body, secret, route):
            logger.warning("[Webhook] invalid signature route=%s", route_name)
            return 401, {"error": "Invalid signature"}

        if not self._take_rate_limit_token(route_name):
            return 429, {"error": "Rate limit exceeded"}

        payload = self._parse_payload(body)
        if payload is None:
            return 400, {"error": "Cannot parse body"}

        event_type = self._resolve_event_type(normalized_headers, payload)
        allowed_events = route.get("events")
        if isinstance(allowed_events, list) and allowed_events and event_type not in {str(x) for x in allowed_events}:
            return 200, {"status": "ignored", "event": event_type}

        delivery_id = self._resolve_delivery_id(normalized_headers)
        if self._is_duplicate_delivery(delivery_id):
            return 200, {"status": "duplicate", "delivery_id": delivery_id}

        prompt = self._render_prompt(
            str(route.get("prompt") or ""),
            payload=payload,
            event_type=event_type,
            route_name=route_name,
        )

        deliver = str(route.get("deliver") or "log").strip() or "log"
        deliver_extra = route.get("deliver_extra") if isinstance(route.get("deliver_extra"), Mapping) else {}
        target_chat_id = str(deliver_extra.get("chat_id") or "").strip()
        target_thread_ts = str(deliver_extra.get("thread_ts") or "").strip() or None

        if bool(route.get("deliver_only")):
            if deliver == "log":
                logger.info("[Webhook] direct log route=%s event=%s delivery=%s text=%s", route_name, event_type, delivery_id, prompt[:500])
                return 200, {
                    "status": "delivered",
                    "route": route_name,
                    "target": "log",
                    "delivery_id": delivery_id,
                }

            if not target_chat_id:
                logger.warning("[Webhook] deliver_only route=%s missing deliver_extra.chat_id", route_name)
                return 422, {"error": "deliver_extra.chat_id is required when deliver_only is enabled"}

            await self.bus.publish_outbound(
                OutboundMessage(
                    channel_name=deliver,
                    chat_id=target_chat_id,
                    thread_id="",
                    text=prompt,
                    thread_ts=target_thread_ts,
                    metadata={
                        "source_channel": "webhook",
                        "route_name": route_name,
                        "delivery_id": delivery_id,
                        "event_type": event_type,
                    },
                )
            )
            return 200, {
                "status": "delivered",
                "route": route_name,
                "target": deliver,
                "delivery_id": delivery_id,
            }

        session_chat_id = f"webhook:{route_name}:{delivery_id}"
        metadata: dict[str, Any] = {
            "event_type": event_type,
            "delivery_id": delivery_id,
            "route_name": route_name,
            "raw_payload": payload,
        }

        if deliver != "log" and target_chat_id:
            metadata["reply_target"] = {
                "channel_name": deliver,
                "chat_id": target_chat_id,
                "thread_ts": target_thread_ts,
            }

        inbound = InboundMessage(
            channel_name=self.name,
            chat_id=session_chat_id,
            user_id=f"webhook:{route_name}",
            text=prompt,
            metadata=metadata,
        )
        await self.bus.publish_inbound(inbound)

        return 202, {
            "status": "accepted",
            "route": route_name,
            "event": event_type,
            "delivery_id": delivery_id,
            "target": deliver if deliver != "log" else "log",
        }

    def _load_routes(self) -> dict[str, dict[str, Any]]:
        routes = self.config.get("routes")
        if not isinstance(routes, Mapping):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in routes.items():
            if isinstance(value, Mapping):
                normalized[str(key)] = dict(value)
        return normalized

    def _validate_routes(self, routes: dict[str, dict[str, Any]]) -> None:
        for route_name, route in routes.items():
            secret = str(route.get("secret") or "").strip()
            if not secret:
                raise ValueError(f"[webhook] route '{route_name}' has no secret")

            if bool(route.get("deliver_only")):
                deliver = str(route.get("deliver") or "log").strip() or "log"
                deliver_extra = route.get("deliver_extra") if isinstance(route.get("deliver_extra"), Mapping) else {}
                target_chat_id = str(deliver_extra.get("chat_id") or "").strip()
                if deliver != "log" and not target_chat_id:
                    raise ValueError(
                        f"[webhook] route '{route_name}' has deliver_only=true but missing deliver_extra.chat_id"
                    )

    def _take_rate_limit_token(self, route_name: str) -> bool:
        now = time.time()
        window = self._rate_windows.setdefault(route_name, [])
        window[:] = [t for t in window if now - t < 60]
        if len(window) >= self._rate_limit_per_minute:
            return False
        window.append(now)
        return True

    def _is_duplicate_delivery(self, delivery_id: str) -> bool:
        now = time.time()
        cutoff = now - self._idempotency_ttl_seconds
        self._seen_deliveries = {k: t for k, t in self._seen_deliveries.items() if t >= cutoff}
        if delivery_id in self._seen_deliveries:
            return True
        self._seen_deliveries[delivery_id] = now
        return False

    @staticmethod
    def _parse_payload(body: bytes) -> dict[str, Any] | None:
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                return payload
            return {"payload": payload}
        except json.JSONDecodeError:
            try:
                return dict(parse_qsl(body.decode("utf-8")))
            except Exception:
                return None

    @staticmethod
    def _resolve_event_type(headers: Mapping[str, str], payload: Mapping[str, Any]) -> str:
        return (
            str(headers.get("x-github-event") or "").strip()
            or str(headers.get("x-gitee-event") or "").strip()
            or str(payload.get("event_type") or "").strip()
            or str(payload.get("hook_name") or "").strip()
            or "unknown"
        )

    @staticmethod
    def _resolve_delivery_id(headers: Mapping[str, str]) -> str:
        return (
            str(headers.get("x-github-delivery") or "").strip()
            or str(headers.get("x-gitee-delivery") or "").strip()
            or str(headers.get("x-request-id") or "").strip()
            or str(int(time.time() * 1000))
        )

    def _validate_signature(
        self,
        headers: Mapping[str, str],
        body: bytes,
        secret: str,
        route: Mapping[str, Any],
    ) -> bool:
        github_signature = headers.get("x-hub-signature-256", "")
        if github_signature:
            expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(github_signature, expected)

        gitee_token = headers.get("x-gitee-token", "")
        if gitee_token:
            mode = str(route.get("gitee_signature_mode") or "auto").strip().lower()
            timestamp = headers.get("x-gitee-timestamp", "")

            if mode in ("auto", "plain") and hmac.compare_digest(gitee_token, secret):
                return True

            if mode in ("auto", "hmac") and timestamp:
                expected = self._build_gitee_hmac_signature(timestamp=timestamp, secret=secret)
                if hmac.compare_digest(gitee_token, expected):
                    return True
                if hmac.compare_digest(unquote_plus(gitee_token), unquote_plus(expected)):
                    return True

            return False

        generic_signature = headers.get("x-webhook-signature", "")
        if generic_signature:
            expected_generic = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(generic_signature, expected_generic)

        logger.debug("[Webhook] secret configured but no supported signature header found")
        return False

    @staticmethod
    def _build_gitee_hmac_signature(*, timestamp: str, secret: str) -> str:
        payload = f"{timestamp}\n{secret}".encode()
        digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        return quote_plus(base64.b64encode(digest).decode("utf-8"))

    def _render_prompt(
        self,
        template: str,
        *,
        payload: Mapping[str, Any],
        event_type: str,
        route_name: str,
    ) -> str:
        if not template:
            raw = json.dumps(payload, ensure_ascii=False, indent=2)
            return f"Webhook event '{event_type}' on route '{route_name}':\n\n```json\n{raw[:4000]}\n```"

        def _resolve_field(path: str) -> str:
            if path == "__raw__":
                return json.dumps(payload, ensure_ascii=False, indent=2)[:4000]
            if path == "event_type":
                return event_type
            if path == "route_name":
                return route_name

            current: Any = payload
            for key in path.split("."):
                if isinstance(current, Mapping) and key in current:
                    current = current[key]
                else:
                    return ""
            if current is None:
                return ""
            if isinstance(current, (dict, list)):
                return json.dumps(current, ensure_ascii=False)
            return str(current)

        return re.sub(r"\{([A-Za-z0-9_.-]+)\}", lambda m: _resolve_field(m.group(1)), template)
