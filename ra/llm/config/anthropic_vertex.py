"""Anthropic Vertex AI client configuration."""

from __future__ import annotations

import os

from llm.config.base import LLMConfig


class AnthropicVertexConfig(LLMConfig):
    """Config for AnthropicVertexClient.

    Defaults read from environment variables matching the Anthropic SDK convention.
    """

    project_id: str = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    region: str = os.environ.get("CLOUD_ML_REGION", "us-east5")
