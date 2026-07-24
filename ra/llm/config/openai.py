"""OpenAI client configuration."""

from __future__ import annotations

import os

from llm.config.base import LLMConfig


class OpenAIConfig(LLMConfig):
    """Config for OpenAIClient.

    Defaults read from environment variables matching the OpenAI SDK convention.
    """

    api_key: str = os.environ.get("OPENAI_API_KEY", "")
    base_url: str = os.environ.get("OPENAI_BASE_URL", "")
