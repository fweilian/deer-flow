"""Tests for webhook channel (GitHub/Gitee ingest and routing)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

from app.channels.message_bus import MessageBus, OutboundMessage
from app.channels.webhook import WebhookChannel


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _github_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class TestWebhookChannel:
    def test_github_llm_flow_accepts_and_enqueues_inbound(self):
        async def go():
            bus = MessageBus()
            channel = WebhookChannel(
                bus=bus,
                config={
                    "routes": {
                        "github_push": {
                            "secret": "top-secret",
                            "events": ["push"],
                            "prompt": "Repo={repository.full_name}; Event={event_type}",
                            "deliver": "telegram",
                            "deliver_extra": {"chat_id": "42"},
                        }
                    }
                },
            )
            await channel.start()

            payload = {"repository": {"full_name": "acme/repo"}}
            body = json.dumps(payload).encode("utf-8")
            headers = {
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "delivery-1",
                "X-Hub-Signature-256": _github_signature("top-secret", body),
            }

            status, resp = await channel.handle_webhook_request(
                "github_push",
                headers=headers,
                body=body,
                content_length=len(body),
            )

            assert status == 202
            assert resp["status"] == "accepted"
            assert resp["target"] == "telegram"

            inbound = await asyncio.wait_for(bus.get_inbound(), timeout=1)
            assert inbound.channel_name == "webhook"
            assert inbound.metadata["delivery_id"] == "delivery-1"
            assert inbound.metadata["reply_target"]["channel_name"] == "telegram"
            assert inbound.metadata["reply_target"]["chat_id"] == "42"
            assert "Repo=acme/repo" in inbound.text

            await channel.stop()

        _run(go())

    def test_default_target_is_log_when_not_configured(self):
        async def go():
            bus = MessageBus()
            channel = WebhookChannel(
                bus=bus,
                config={
                    "routes": {
                        "github_push": {
                            "secret": "top-secret",
                            "events": ["push"],
                        }
                    }
                },
            )
            await channel.start()

            body = b"{}"
            headers = {
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "delivery-2",
                "X-Hub-Signature-256": _github_signature("top-secret", body),
            }
            status, resp = await channel.handle_webhook_request("github_push", headers=headers, body=body, content_length=len(body))

            assert status == 202
            assert resp["target"] == "log"

            inbound = await asyncio.wait_for(bus.get_inbound(), timeout=1)
            assert "reply_target" not in inbound.metadata

            await channel.stop()

        _run(go())

    def test_invalid_signature_rejected(self):
        async def go():
            bus = MessageBus()
            channel = WebhookChannel(
                bus=bus,
                config={
                    "routes": {
                        "github_push": {
                            "secret": "top-secret",
                        }
                    }
                },
            )
            await channel.start()

            body = b"{}"
            status, resp = await channel.handle_webhook_request(
                "github_push",
                headers={
                    "X-Hub-Signature-256": "sha256=bad",
                    "X-GitHub-Delivery": "delivery-bad",
                },
                body=body,
                content_length=len(body),
            )

            assert status == 401
            assert resp["error"] == "Invalid signature"

            await channel.stop()

        _run(go())

    def test_duplicate_delivery_ignored(self):
        async def go():
            bus = MessageBus()
            channel = WebhookChannel(
                bus=bus,
                config={
                    "routes": {
                        "github_push": {
                            "secret": "top-secret",
                        }
                    }
                },
            )
            await channel.start()

            body = b"{}"
            headers = {
                "X-Hub-Signature-256": _github_signature("top-secret", body),
                "X-GitHub-Delivery": "dup-delivery",
            }

            status1, _ = await channel.handle_webhook_request("github_push", headers=headers, body=body, content_length=len(body))
            status2, resp2 = await channel.handle_webhook_request("github_push", headers=headers, body=body, content_length=len(body))

            assert status1 == 202
            assert status2 == 200
            assert resp2["status"] == "duplicate"

            first = await asyncio.wait_for(bus.get_inbound(), timeout=1)
            assert first.metadata["delivery_id"] == "dup-delivery"
            assert bus.inbound_queue.empty()

            await channel.stop()

        _run(go())

    def test_deliver_only_sends_to_target_channel(self):
        async def go():
            bus = MessageBus()
            channel = WebhookChannel(
                bus=bus,
                config={
                    "routes": {
                        "github_alert": {
                            "secret": "top-secret",
                            "deliver_only": True,
                            "deliver": "telegram",
                            "deliver_extra": {"chat_id": "777"},
                            "prompt": "Alert {event_type}",
                        }
                    }
                },
            )
            await channel.start()

            sent: list[OutboundMessage] = []

            async def collect(msg: OutboundMessage):
                sent.append(msg)

            bus.subscribe_outbound(collect)

            body = b"{}"
            headers = {
                "X-Hub-Signature-256": _github_signature("top-secret", body),
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "delivery-3",
            }
            status, resp = await channel.handle_webhook_request("github_alert", headers=headers, body=body, content_length=len(body))

            assert status == 200
            assert resp["status"] == "delivered"
            assert len(sent) == 1
            assert sent[0].channel_name == "telegram"
            assert sent[0].chat_id == "777"
            assert "Alert issues" in sent[0].text
            assert bus.inbound_queue.empty()

            await channel.stop()

        _run(go())

    def test_gitee_plain_signature_supported(self):
        async def go():
            bus = MessageBus()
            channel = WebhookChannel(
                bus=bus,
                config={
                    "routes": {
                        "gitee_push": {
                            "secret": "my-secret",
                            "events": ["Push Hook"],
                            "gitee_signature_mode": "plain",
                        }
                    }
                },
            )
            await channel.start()

            body = b"{}"
            headers = {
                "X-Gitee-Token": "my-secret",
                "X-Gitee-Event": "Push Hook",
                "X-Gitee-Delivery": "gitee-1",
            }
            status, resp = await channel.handle_webhook_request("gitee_push", headers=headers, body=body, content_length=len(body))

            assert status == 202
            assert resp["status"] == "accepted"

            inbound = await asyncio.wait_for(bus.get_inbound(), timeout=1)
            assert inbound.metadata["event_type"] == "Push Hook"

            await channel.stop()

        _run(go())

    def test_route_validation_rejects_bad_deliver_only_config(self):
        async def go():
            channel = WebhookChannel(
                bus=MessageBus(),
                config={
                    "routes": {
                        "bad": {
                            "secret": "abc",
                            "deliver_only": True,
                            "deliver": "telegram",
                            "deliver_extra": {},
                        }
                    }
                },
            )
            try:
                await channel.start()
            except ValueError as exc:
                assert "deliver_extra.chat_id" in str(exc)
            else:
                raise AssertionError("expected ValueError for invalid route config")

        _run(go())
