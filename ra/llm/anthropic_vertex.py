"""AnthropicVertex LLMClient implementation."""

from __future__ import annotations

from typing import Any

import anthropic
from anthropic.types import ToolUseBlock

from llm.base import LLMClient
from llm.types import LLMResponse, ToolCall


class AnthropicVertexClient(LLMClient):
    """LLMClient backed by Anthropic's Vertex AI Model Garden.

    Uses streaming (non-streaming times out with high max_tokens)
    and extended thinking.
    """

    def __init__(self) -> None:
        # SDK reads ANTHROPIC_VERTEX_PROJECT_ID and CLOUD_ML_REGION from env
        self._client = anthropic.AnthropicVertex()

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        system: str,
        thinking_budget: int,
    ) -> LLMResponse:
        with self._client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "enabled", "budget_tokens": thinking_budget},
            system=system,
            tools=tools,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        return _parse_response(response)


def _parse_response(response: anthropic.types.Message) -> LLMResponse:
    """Normalize an Anthropic response into LLMResponse."""
    content_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in response.content:
        if isinstance(block, ToolUseBlock):
            arguments = block.input if isinstance(block.input, dict) else {}
            tool_calls.append(ToolCall(
                id=block.id,
                name=block.name,
                arguments=arguments,
            ))
        elif hasattr(block, "text"):
            content_parts.append(block.text)

    return LLMResponse(
        content="\n".join(content_parts) if content_parts else None,
        tool_calls=tool_calls,
        stop_reason=response.stop_reason,
        tokens_in=response.usage.input_tokens,
        tokens_out=response.usage.output_tokens,
    )
