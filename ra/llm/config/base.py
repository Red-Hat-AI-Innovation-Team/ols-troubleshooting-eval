"""Base LLM client configuration."""

from __future__ import annotations

from pydantic import BaseModel


class LLMConfig(BaseModel):
    """Base config shared by all LLM client implementations."""

    max_concurrency: int = 5
