"""Debug logging helpers for OpenAI-compatible chat completions models."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI

_ENV_FLAG = "DEERFLOW_OPENAI_HTTP_DEBUG"
_TRUNCATE_AT = 20000


def _debug_enabled() -> bool:
    value = os.getenv(_ENV_FLAG, "")
    return value.strip().lower() in {"1", "true", "yes", "on", "debug"}


def _safe_json(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        text = repr(value)
    if len(text) > _TRUNCATE_AT:
        return f"{text[:_TRUNCATE_AT]}...<truncated {len(text) - _TRUNCATE_AT} chars>"
    return text


def log_chat_completions_request(
    logger: logging.Logger,
    payload: dict[str, Any],
    *,
    provider_name: str,
    model_name: str | None,
) -> None:
    if not _debug_enabled():
        return
    logger.debug(
        "OpenAI chat request POST /v1/chat/completions provider=%s model=%s payload=%s",
        provider_name,
        model_name or "",
        _safe_json(payload),
    )


def log_chat_completions_response(
    logger: logging.Logger,
    response: Any,
    *,
    provider_name: str,
    model_name: str | None,
) -> None:
    if not _debug_enabled():
        return
    response_dict = response if isinstance(response, dict) else response.model_dump()
    logger.debug(
        "OpenAI chat response POST /v1/chat/completions provider=%s model=%s response=%s",
        provider_name,
        model_name or "",
        _safe_json(response_dict),
    )


class DebugChatOpenAI(ChatOpenAI):
    """ChatOpenAI variant that prints debug logs for request/response payloads."""

    model_config = {"arbitrary_types_allowed": True}

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        log_chat_completions_request(
            logging.getLogger(__name__),
            payload,
            provider_name=type(self).__name__,
            model_name=getattr(self, "model_name", None) or getattr(self, "model", None),
        )
        return payload

    def _create_chat_result(
        self,
        response: dict | Any,
        generation_info: dict | None = None,
    ) -> ChatResult:
        log_chat_completions_response(
            logging.getLogger(__name__),
            response,
            provider_name=type(self).__name__,
            model_name=getattr(self, "model_name", None) or getattr(self, "model", None),
        )
        return super()._create_chat_result(response, generation_info=generation_info)
