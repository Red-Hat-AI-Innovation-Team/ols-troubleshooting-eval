"""verl reward function for OLS troubleshooting.

Scores the model's final diagnosis against the expected answer using an
LLM-as-judge (gpt-5-mini by default).

Signature matches verl's custom reward function interface::

    compute_score(data_source, solution_str, ground_truth, extra_info=None) -> float
"""

import os
import re

import openai


def compute_score(data_source, solution_str, ground_truth, extra_info=None):
    """Score the model's final diagnosis against the expected answer.

    Args:
        data_source: dataset name (unused).
        solution_str: the model's full response text.
        ground_truth: the expected response from the dataset.
        extra_info: dict with additional metadata (query, scenario_id, etc.).

    Returns:
        float score 0.0 to 1.0.
    """
    client = openai.OpenAI()

    query = extra_info.get("query", "") if extra_info else ""

    prompt = f"""Evaluate the answer correctness of the given response.

Question: {query}
Response: {solution_str}
Expected Response: {ground_truth}

Consider:
- Factual accuracy compared to expected response
- Completeness of information
- Alignment with expected response
- Absence of contradictory information

Rate the answer correctness and provide your reasoning.

Format your response as:
Score: [your score on a scale of 0.0 to 1.0]
Reason: [your detailed explanation]"""

    response = client.chat.completions.create(
        model=os.environ.get("JUDGE_MODEL", "gpt-5-mini"),
        messages=[
            {"role": "system", "content": "You are an evaluation judge."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=500,
        temperature=0,
    )

    text = response.choices[0].message.content or ""

    score_match = re.search(r"Score:\s*([\d.]+)", text)
    if score_match:
        score = float(score_match.group(1))
        if score > 1.0:
            score = score / 10.0 if score <= 10.0 else score / 100.0
        return min(1.0, max(0.0, score))

    return 0.0
