"""OpenAI chat completions LLMClient implementation."""

from __future__ import annotations

import json
from typing import Any

import openai
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall

from llm.base import LLMClient
from llm.types import LLMResponse, Message, ToolCall, ToolDef


class OpenAIClient(LLMClient):
    """LLMClient backed by OpenAI chat completions API.

    thinking_budget is ignored (not supported by OpenAI).
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        # SDK reads OPENAI_API_KEY from env when api_key is None
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url)

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
            messages=_to_openai_messages(messages, system),
            max_completion_tokens=max_tokens,
        )
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)

        response = self._client.chat.completions.create(**kwargs)
        return _parse_response(response)


# ---------------------------------------------------------------------------
# Internal → OpenAI format
# ---------------------------------------------------------------------------

def _to_openai_tools(tools: list[ToolDef]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _to_openai_messages(messages: list[Message], system: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]

    for msg in messages:
        if msg.role == "tool":
            for tr in msg.tool_results:
                out.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_call_id,
                    "content": tr.content,
                })
        elif msg.role == "assistant" and msg.tool_calls:
            oai_msg: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            out.append(oai_msg)
        else:
            out.append({"role": msg.role, "content": msg.content or ""})

    return out


# ---------------------------------------------------------------------------
# OpenAI response → internal types
# ---------------------------------------------------------------------------

def _parse_response(response: openai.types.chat.ChatCompletion) -> LLMResponse:
    choice = response.choices[0]
    msg = choice.message

    tool_calls: list[ToolCall] = []
    if msg.tool_calls:
        for tc in msg.tool_calls:
            if not isinstance(tc, ChatCompletionMessageToolCall):
                continue
            arguments = json.loads(tc.function.arguments) if tc.function.arguments else {}
            tool_calls.append(ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=arguments,
            ))

    return LLMResponse(
        content=msg.content,
        tool_calls=tool_calls,
        stop_reason=choice.finish_reason,
        tokens_in=response.usage.prompt_tokens if response.usage else 0,
        tokens_out=response.usage.completion_tokens if response.usage else 0,
    )
