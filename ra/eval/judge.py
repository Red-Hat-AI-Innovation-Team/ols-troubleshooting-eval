"""Standalone LLM judge for answer correctness evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from llm.base import LLMClient
from llm.types import Message


ANSWER_CORRECTNESS_PROMPT = """Evaluate the answer correctness of the given response.

Question: {query}
Response: {response}
Expected Response: {expected_response}

Consider:
- Factual accuracy compared to expected response
- Completeness of information
- Alignment with expected response
- Absence of contradictory information

Rate the answer correctness and provide your reasoning.

Format your response as:
Score: [your score on a scale of 0.0 to 1.0]
Reason: [your detailed explanation]"""


@dataclass
class JudgeResult:
    """Result from the LLM judge."""

    score: float
    reason: str
    raw_response: str


def _parse_score(text: str) -> float:
    """Parse a score from judge response, normalizing to 0-1 range.

    Handles formats: 'Score: 0.8', 'Score: 8/10', 'Score: 8 out of 10'.
    """
    # Try "Score: X/Y" or "Score: X out of Y"
    m = re.search(r"Score:\s*([\d.]+)\s*(?:/|out\s+of)\s*([\d.]+)", text, re.IGNORECASE)
    if m:
        numerator = float(m.group(1))
        denominator = float(m.group(2))
        if denominator > 0:
            return min(max(numerator / denominator, 0.0), 1.0)

    # Try "Score: X.Y" (direct 0-1 scale)
    m = re.search(r"Score:\s*([\d.]+)", text, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        # If value > 1, assume it's on a 0-10 scale
        if val > 1.0:
            val = val / 10.0
        return min(max(val, 0.0), 1.0)

    return 0.0


def _parse_reason(text: str) -> str:
    """Extract the reason from judge response."""
    m = re.search(r"Reason:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def judge(
    query: str,
    response: str,
    expected_response: str,
    client: LLMClient,
    model: str,
) -> JudgeResult:
    """Evaluate answer correctness using an LLM judge.

    Args:
        query: The original question.
        response: The model's response to evaluate.
        expected_response: The expected/reference response.
        client: An LLMClient instance for making LLM calls.
        model: The model name to use for judging.

    Returns:
        JudgeResult with score (0-1), reason, and raw response.
    """
    prompt = ANSWER_CORRECTNESS_PROMPT.format(
        query=query,
        response=response,
        expected_response=expected_response,
    )

    llm_response = client.chat(
        model=model,
        messages=[Message(role="user", content=prompt)],
        tools=[],
        max_tokens=1024,
        system="You are an evaluation judge.",
        thinking_budget=0,
    )

    raw = llm_response.content or ""
    score = _parse_score(raw)
    reason = _parse_reason(raw)

    return JudgeResult(score=score, reason=reason, raw_response=raw)
