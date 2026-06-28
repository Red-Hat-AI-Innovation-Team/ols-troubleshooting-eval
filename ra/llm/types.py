"""Provider-agnostic types for the LLMClient abstraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolDef:
    """A tool definition (provider-agnostic)."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class ToolCall:
    """A tool invocation returned by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """Result of executing a tool call."""

    tool_call_id: str
    content: str


Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class Message:
    """A single message in a conversation.

    role:
        "system"    — system prompt
        "user"      — user text (content set)
        "assistant" — model reply (content and/or tool_calls set)
        "tool"      — tool execution results (tool_results set)
    """

    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)


@dataclass
class LLMResponse:
    """Normalized response from any provider."""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
