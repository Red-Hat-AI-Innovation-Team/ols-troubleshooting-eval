"""LLMClient abstract base class."""

from __future__ import annotations

from abc import ABC, abstractmethod

from llm.types import LLMResponse, Message, ToolDef


class LLMClient(ABC):
    """Provider-agnostic interface for chat-with-tools.

    Each implementation translates internal types (Message, ToolDef)
    to provider format at the edge, calls the API, and normalizes
    the response back to LLMResponse.
    """

    @abstractmethod
    def chat(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolDef],
        max_tokens: int,
        system: str,
        thinking_budget: int,
    ) -> LLMResponse: ...
