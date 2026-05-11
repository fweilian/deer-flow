from __future__ import annotations

from langchain_core.messages import HumanMessage

from deerflow.models.openai_debug import DebugChatOpenAI


def _make_model() -> DebugChatOpenAI:
    return DebugChatOpenAI(
        model="gpt-test",
        api_key="test-key",
        base_url="https://example.com/v1",
    )


def test_debug_chat_openai_logs_request_payload(monkeypatch, caplog):
    monkeypatch.setenv("DEERFLOW_OPENAI_HTTP_DEBUG", "1")
    model = _make_model()

    with caplog.at_level("DEBUG", logger="deerflow.models.openai_debug"):
        payload = model._get_request_payload([HumanMessage(content="hello cron")])

    assert payload["messages"][0]["content"] == "hello cron"
    assert "OpenAI chat request POST /v1/chat/completions" in caplog.text
    assert '"content": "hello cron"' in caplog.text


def test_debug_chat_openai_logs_response_payload(monkeypatch, caplog):
    monkeypatch.setenv("DEERFLOW_OPENAI_HTTP_DEBUG", "1")
    model = _make_model()

    with caplog.at_level("DEBUG", logger="deerflow.models.openai_debug"):
        result = model._create_chat_result(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "分析报告正文",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "model": "gpt-test",
            }
        )

    assert result.generations[0].message.content == "分析报告正文"
    assert "OpenAI chat response POST /v1/chat/completions" in caplog.text
    assert '"content": "分析报告正文"' in caplog.text
