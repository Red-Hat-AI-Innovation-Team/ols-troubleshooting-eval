"""LLMClient abstract base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from llm.types import LLMResponse


class LLMClient(ABC):
    """Provider-agnostic interface for chat-with-tools.

    Each implementation:
    1. Translates tool defs + messages to provider format
    2. Calls the provider API
    3. Normalizes provider response -> LLMResponse
    """

    @abstractmethod
    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        system: str,
        thinking_budget: int,
    ) -> LLMResponse: ...
