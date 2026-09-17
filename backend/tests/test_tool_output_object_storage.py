"""The tool-output budget persists oversized output through shared outputs."""

from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares import tool_output_budget_middleware as middleware
from deerflow.config.tool_output_config import ToolOutputConfig


class _Outputs:
    def __init__(self) -> None:
        self.writes: dict[str, bytes] = {}

    async def write_bytes(self, relative_path: str, content: bytes, *, content_type: str | None = None):  # noqa: ARG002
        self.writes[relative_path] = content

    def virtual_path(self, relative_path: str) -> str:
        return f"/mnt/user-data/outputs/{relative_path}"


@pytest.mark.asyncio
async def test_oversized_tool_output_is_externalized_to_shared_outputs() -> None:
    outputs = _Outputs()
    result = await middleware._patch_result_async(
        ToolMessage(content="x" * 200, name="web_fetch", tool_call_id="call-1"),
        ToolOutputConfig(externalize_min_chars=50, preview_head_chars=10, preview_tail_chars=10),
        outputs=outputs,
        sandbox=None,
    )

    assert "Full web_fetch output saved to" in result.content
    assert len(outputs.writes) == 1
    relative_path, content = next(iter(outputs.writes.items()))
    assert relative_path.startswith(".tool-results/web_fetch-")
    assert content == b"x" * 200
