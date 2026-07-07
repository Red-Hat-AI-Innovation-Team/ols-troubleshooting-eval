"""Provider-agnostic types for the LLMClient abstraction."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ToolDef(BaseModel):
    """A tool definition (provider-agnostic)."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


class ToolCall(BaseModel):
    """A tool invocation returned by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


class ToolResult(BaseModel):
    """Result of executing a tool call."""

    tool_call_id: str
    content: str


Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    """A single message in a conversation.

    role:
        "system"    — system prompt
        "user"      — user text (content set)
        "assistant" — model reply (content and/or tool_calls set)
        "tool"      — tool execution results (tool_results set)
    """

    role: Role
    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[ToolCall] = []
    tool_results: list[ToolResult] = []


class LLMResponse(BaseModel):
    """Normalized response from any provider."""

    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[ToolCall] = []
    stop_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
