"""AnthropicVertex LLMClient implementation."""

from __future__ import annotations

import json
from typing import Any

import anthropic
from anthropic.types import ToolUseBlock

from llm.base import LLMClient
from llm.types import LLMResponse, Message, ToolCall, ToolDef


class AnthropicVertexClient(LLMClient):
    """LLMClient backed by Anthropic's Vertex AI Model Garden.

    Uses streaming (non-streaming times out with high max_tokens)
    and extended thinking.
    """

    def __init__(self) -> None:
        # SDK reads ANTHROPIC_VERTEX_PROJECT_ID and CLOUD_ML_REGION from env
        self._client = anthropic.AnthropicVertex()

    def _chat_impl(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolDef],
        max_tokens: int,
        system: str,
        thinking_budget: int,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "enabled", "budget_tokens": thinking_budget},
            system=system,
            messages=_to_anthropic_messages(messages),
        )
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)

        with self._client.messages.stream(**kwargs) as stream:
            response = stream.get_final_message()

        return _parse_response(response)


# ---------------------------------------------------------------------------
# Internal → Anthropic format
# ---------------------------------------------------------------------------

def _to_anthropic_tools(tools: list[ToolDef]) -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
        }
        for t in tools
    ]


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "tool":
            out.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tr.tool_call_id,
                        "content": tr.content,
                    }
                    for tr in msg.tool_results
                ],
            })
        elif msg.role == "assistant" and msg.tool_calls:
            content: list[dict[str, Any]] = []
            if msg.content:
                content.append({"type": "text", "text": msg.content})
            for tc in msg.tool_calls:
                content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                })
            out.append({"role": "assistant", "content": content})
        elif msg.role == "assistant":
            content_blocks: list[dict[str, Any]] = []
            if msg.content:
                content_blocks.append({"type": "text", "text": msg.content})
            out.append({"role": "assistant", "content": content_blocks or msg.content})
        else:
            out.append({"role": msg.role, "content": msg.content or ""})
    return out


# ---------------------------------------------------------------------------
# Anthropic response → internal types
# ---------------------------------------------------------------------------

def _parse_response(response: anthropic.types.Message) -> LLMResponse:
    content_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in response.content:
        if isinstance(block, ToolUseBlock):
            if isinstance(block.input, dict):
                arguments = block.input
            elif isinstance(block.input, str):
                arguments = json.loads(block.input)
            else:
                arguments = {}
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
