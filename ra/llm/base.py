"""LLMClient abstract base class with tenacity retry."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

import anthropic
import httpx
import openai
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
    before_sleep_log,
)

from llm.config.base import LLMConfig
from llm.types import LLMResponse, Message, ToolDef

logger = logging.getLogger(__name__)

# Exceptions that should trigger a retry
RETRYABLE_EXCEPTIONS = (
    # Rate limits / transient server errors
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.APIStatusError,
    openai.RateLimitError,
    openai.InternalServerError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    # httpx transport errors (leak through SDK during streaming)
    httpx.RemoteProtocolError,
    # Data parsing errors (LLM returned malformed output)
    json.JSONDecodeError,
    ValueError,
    ValidationError,
    KeyError,
    IndexError,
)

_chat_retry = retry(
    retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
    stop=stop_after_attempt(10),
    wait=wait_exponential_jitter(initial=5, max=120, jitter=5),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


class LLMClient(ABC):
    """Provider-agnostic interface for chat-with-tools.

    Each implementation translates internal types (Message, ToolDef)
    to provider format at the edge, calls the API, and normalizes
    the response back to LLMResponse.

    The public chat() method wraps _chat_impl() with tenacity retry
    (exponential backoff + jitter, max 10 attempts).
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @_chat_retry
    def chat(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolDef],
        max_tokens: int,
        system: str,
        thinking_budget: int,
    ) -> LLMResponse:
        return self._chat_impl(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            system=system,
            thinking_budget=thinking_budget,
        )

    @abstractmethod
    def _chat_impl(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolDef],
        max_tokens: int,
        system: str,
        thinking_budget: int,
    ) -> LLMResponse: ...
