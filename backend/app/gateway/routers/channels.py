"""Gateway router for IM channel management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels", tags=["channels"])


class ChannelStatusResponse(BaseModel):
    service_running: bool
    channels: dict[str, dict]


class ChannelRestartResponse(BaseModel):
    success: bool
    message: str


class WebhookResponse(BaseModel):
    status: str | None = None
    error: str | None = None
    route: str | None = None
    event: str | None = None
    target: str | None = None
    delivery_id: str | None = None


@router.get("/", response_model=ChannelStatusResponse)
async def get_channels_status() -> ChannelStatusResponse:
    """Get the status of all IM channels."""
    from app.channels.service import get_channel_service

    service = get_channel_service()
    if service is None:
        return ChannelStatusResponse(service_running=False, channels={})
    status = service.get_status()
    return ChannelStatusResponse(**status)


@router.post("/{name}/restart", response_model=ChannelRestartResponse)
async def restart_channel(name: str) -> ChannelRestartResponse:
    """Restart a specific IM channel."""
    from app.channels.service import get_channel_service

    service = get_channel_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Channel service is not running")

    success = await service.restart_channel(name)
    if success:
        logger.info("Channel %s restarted successfully", name)
        return ChannelRestartResponse(success=True, message=f"Channel {name} restarted successfully")
    else:
        logger.warning("Failed to restart channel %s", name)
        return ChannelRestartResponse(success=False, message=f"Failed to restart channel {name}")


@router.post("/webhook/{route_name}", response_model=WebhookResponse)
async def receive_webhook(route_name: str, request: Request) -> JSONResponse:
    """Receive GitHub/Gitee webhook requests through the webhook channel."""
    from app.channels.service import get_channel_service

    service = get_channel_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Channel service is not running")

    webhook_channel = service.get_channel("webhook")
    if webhook_channel is None:
        raise HTTPException(status_code=503, detail="Webhook channel is not running")

    handler = getattr(webhook_channel, "handle_webhook_request", None)
    if not callable(handler):
        raise HTTPException(status_code=500, detail="Webhook channel handler is unavailable")

    body = await request.body()
    content_length: int | None = None
    content_length_header = request.headers.get("content-length")
    if content_length_header:
        try:
            content_length = int(content_length_header)
        except ValueError:
            content_length = None

    status_code, payload = await handler(
        route_name,
        headers=request.headers,
        body=body,
        content_length=content_length,
    )
    if not isinstance(payload, dict):
        payload = {"error": "Invalid webhook response payload"}
        status_code = 500

    return JSONResponse(status_code=status_code, content=payload)
