"""Nemotron vLLM thinking-budget client (two-step approach).

Implements the budget-controlled reasoning pattern from the NVIDIA
Nemotron-3-Nano-30B HF model card.  Step 1 caps reasoning tokens via a
chat-completions call; step 2 continues generation through the raw
completions endpoint using the tokenizer's chat template with
``continue_final_message=True``.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from transformers import AutoTokenizer

from llm.config.openai import OpenAIConfig
from llm.openai_client import OpenAIClient, _parse_response, _to_openai_messages, _to_openai_tools
from llm.types import LLMResponse, Message, ToolCall, ToolDef


class NemotronVLLMThinkingClient(OpenAIClient):
    """OpenAI-compatible client with thinking budget control for Nemotron on vLLM.

    Two-step approach (from the HF model card):
      1. ``chat.completions.create`` capped at *thinking_budget* tokens
         to obtain the reasoning trace.
      2. If the trace was truncated (no ``</think>``), close the tag,
         build a raw prompt via ``tokenizer.apply_chat_template`` with
         ``continue_final_message=True``, and call ``completions.create``
         for the answer portion.

    When *thinking_budget <= 0* the model is called once with
    ``enable_thinking: false`` (standard non-reasoning mode).

    Tool calls in the answer are parsed from raw text using the
    *qwen3_coder* ``<tool_call>`` format.
    """

    def __init__(
        self,
        config: OpenAIConfig,
        tokenizer_name_or_path: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16",
    ) -> None:
        super().__init__(config)
        self.tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_name_or_path, trust_remote_code=True,
        )

    # --------------------------------------------------------------------- #
    # Core override
    # --------------------------------------------------------------------- #

    def _chat_impl(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolDef],
        max_tokens: int,
        system: str,
        thinking_budget: int,
    ) -> LLMResponse:
        oai_messages = _to_openai_messages(messages, system)

        # ── No-thinking fast path ────────────────────────────────────────
        if thinking_budget <= 0:
            kwargs: dict[str, Any] = dict(
                model=model,
                messages=oai_messages,
                max_completion_tokens=max_tokens,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            if tools:
                kwargs["tools"] = _to_openai_tools(tools)
            response = self._client.chat.completions.create(**kwargs)
            return _parse_response(response)

        # ── Step 1: reasoning trace (capped at thinking_budget tokens) ───
        step1_kwargs: dict[str, Any] = dict(
            model=model,
            messages=oai_messages,
            max_completion_tokens=thinking_budget,
        )
        if tools:
            step1_kwargs["tools"] = _to_openai_tools(tools)
        step1_response = self._client.chat.completions.create(**step1_kwargs)
        step1_choice = step1_response.choices[0]
        content = step1_choice.message.content or ""

        # Complete response (thinking + answer) fit within budget.
        if step1_choice.finish_reason in ("stop", "tool_calls"):
            return _parse_response(step1_response)

        # ── Step 2: close reasoning, get answer via completions API ──────
        reasoning_content = content
        if "</think>" not in reasoning_content:
            reasoning_content = f"{reasoning_content}.\n</think>\n\n"

        reasoning_tokens = len(
            self.tokenizer.encode(reasoning_content, add_special_tokens=False),
        )
        remaining_tokens = max_tokens - reasoning_tokens
        assert remaining_tokens > 0, (
            f"remaining tokens must be positive ({remaining_tokens=}). "
            f"Increase max_tokens or lower thinking_budget."
        )

        extended = list(oai_messages) + [
            {"role": "assistant", "content": reasoning_content},
        ]

        # (rohan): instead of completions endpoint, use chat completions
        kwargs = dict(
            model=model, messages=extended, max_tokens=remaining_tokens,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        if tools: kwargs['tools'] = _to_openai_tools(tools)
        step2_response = self._client.chat.completions.create(**kwargs)
        step2_parsed = _parse_response(step2_response)
        clean_content = step2_parsed.content or ""
        tool_calls = step2_parsed.tool_calls
        #
        # prompt = self.tokenizer.apply_chat_template(
        #     extended, tokenize=False, continue_final_message=True,
        # )
        #
        # step2_response = self._client.completions.create(
        #     model=model, prompt=prompt, max_tokens=remaining_tokens,
        # )
        # answer_text = step2_response.choices[0].text
        #
        # # Parse qwen3_coder tool calls from raw text
        # tool_calls, clean_content = _parse_tool_calls_from_text(answer_text)

        # Strip <think> tags from reasoning
        clean_reasoning = (
            reasoning_content.replace("<think>", "")
            .replace("</think>", "")
            .strip()
        )

        # Aggregate token counts across both calls
        u1, u2 = step1_response.usage, step2_response.usage
        tokens_in = (u1.prompt_tokens if u1 else 0) + (u2.prompt_tokens if u2 else 0)
        tokens_out = (u1.completion_tokens if u1 else 0) + (u2.completion_tokens if u2 else 0)

        return LLMResponse(
            content=clean_content or None,
            reasoning=clean_reasoning,
            tool_calls=tool_calls,
            stop_reason=step2_response.choices[0].finish_reason,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )


# ---------------------------------------------------------------------- #
# Tool-call parsing from raw completions text (qwen3_coder format)
# ---------------------------------------------------------------------- #

_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)


def _parse_tool_calls_from_text(text: str) -> tuple[list[ToolCall], str]:
    """Extract ``<tool_call>`` blocks and return *(tool_calls, remaining_text)*."""
    matches = _TOOL_CALL_RE.findall(text)
    if not matches:
        return [], text.strip()

    tool_calls: list[ToolCall] = []
    for raw_json in matches:
        parsed = json.loads(raw_json)
        tool_calls.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:24]}",
                name=parsed["name"],
                arguments=parsed.get("arguments", {}),
            ),
        )

    clean = _TOOL_CALL_RE.sub("", text).strip()
    return tool_calls, clean
