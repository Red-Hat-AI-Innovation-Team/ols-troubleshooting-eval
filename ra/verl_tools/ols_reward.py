"""verl reward function for OLS troubleshooting — process-supervised.

Combines per-tool-call process rewards with outcome-based judge scoring.
Implements PPRM outcome-conditioned normalization to prevent reward inflation
on failed trajectories.

Reward components:
  - Information gain (25%): probability shift toward correct diagnosis (optional, requires model)
  - Retrieval existence (15%): ground-truth entities found in tool output
  - Utilization (10%): tool output referenced in next reasoning step (optional, requires embeddings)
  - Format compliance (5%): valid <tool_call> syntax
  - Redundancy penalty (-5%): negative per duplicate (tool, args)
  - Tool count penalty (-2%/call): linear efficiency penalty
  - Outcome (40%): rubric-based judge evaluation

Lightweight components (format, redundancy, tool count, retrieval) always run.
Heavier components (info gain, utilization) are enabled via environment flags.

Signature matches verl's custom reward function interface::

    compute_score(data_source, solution_str, ground_truth, extra_info=None) -> float
"""

import json
import os
import re
from collections import defaultdict

import openai


# ---------------------------------------------------------------------------
# Tool call parsing
# ---------------------------------------------------------------------------

# Match Hermes-style <tool_call>...</tool_call> blocks
TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)

# Match tool response blocks (verl format)
TOOL_RESPONSE_PATTERN = re.compile(
    r"<tool_response>\s*(.*?)\s*</tool_response>",
    re.DOTALL,
)


def parse_tool_calls(solution_str: str) -> list[dict]:
    """Extract tool calls from the model's response.

    Returns list of dicts with keys: name, arguments, raw_json.
    """
    calls = []
    for match in TOOL_CALL_PATTERN.finditer(solution_str):
        raw = match.group(1)
        try:
            parsed = json.loads(raw)
            calls.append({
                "name": parsed.get("name", ""),
                "arguments": parsed.get("arguments", {}),
                "raw_json": raw,
            })
        except json.JSONDecodeError:
            # Malformed JSON — still counts as a tool call attempt
            calls.append({"name": "", "arguments": {}, "raw_json": raw})
    return calls


def parse_tool_responses(solution_str: str) -> list[str]:
    """Extract tool response texts from the conversation."""
    return [m.group(1) for m in TOOL_RESPONSE_PATTERN.finditer(solution_str)]


# ---------------------------------------------------------------------------
# Process reward components (lightweight, always-on)
# ---------------------------------------------------------------------------

def score_format_compliance(solution_str: str) -> float:
    """Binary: does the response contain at least one valid <tool_call> with parseable JSON?

    Returns 1.0 if valid tool calls present, 0.0 otherwise.
    """
    calls = parse_tool_calls(solution_str)
    if not calls:
        return 0.0
    # Check at least one has a valid name
    return 1.0 if any(c["name"] for c in calls) else 0.0


def score_redundancy(solution_str: str) -> float:
    """Penalty for duplicate (tool_name, arguments) pairs.

    Returns negative value: -1.0 per duplicate call.
    """
    calls = parse_tool_calls(solution_str)
    seen: dict[str, int] = {}
    duplicates = 0

    for call in calls:
        # Create a hashable key from tool name + sorted arguments
        try:
            args_key = json.dumps(call["arguments"], sort_keys=True)
        except (TypeError, ValueError):
            args_key = call["raw_json"]
        key = f"{call['name']}:{args_key}"

        if key in seen:
            duplicates += 1
        seen[key] = seen.get(key, 0) + 1

    return -float(duplicates)


def score_tool_count(solution_str: str) -> float:
    """Linear penalty per tool call to encourage efficiency.

    Returns negative value proportional to number of tool calls.
    K8s diagnostics typically need 3-7 calls; >8 gets significant penalty.
    """
    calls = parse_tool_calls(solution_str)
    return -float(len(calls))


def score_retrieval_existence(solution_str: str, ground_truth: str) -> float:
    """Check if tool outputs contain ground-truth diagnostic entities.

    Extracts key entities from the expected response and checks if they
    appear in tool response text. Returns 0.0-1.0 based on coverage.
    """
    if not ground_truth:
        return 0.0

    tool_responses = parse_tool_responses(solution_str)
    if not tool_responses:
        return 0.0

    combined_output = " ".join(tool_responses).lower()

    # Extract entities from ground truth: words >3 chars that look like
    # identifiers (pod names, error codes, namespace names, etc.)
    gt_lower = ground_truth.lower()
    # Split on whitespace and punctuation, keep meaningful tokens
    gt_tokens = re.findall(r'[a-z][a-z0-9_\-]{2,}', gt_lower)
    # Filter out common English words
    stopwords = {
        "the", "and", "for", "are", "but", "not", "you", "all", "can",
        "had", "her", "was", "one", "our", "out", "has", "have", "been",
        "from", "this", "that", "with", "they", "will", "each", "make",
        "like", "just", "over", "such", "than", "them", "very", "when",
        "what", "your", "which", "their", "about", "would", "there",
        "could", "other", "into", "more", "some", "these", "then",
        "also", "because", "due", "possibly", "issues", "between",
        "experienced", "could", "connect", "external", "running",
        "caused", "causing", "connection", "errors", "should", "after",
    }
    entities = [t for t in gt_tokens if t not in stopwords]

    if not entities:
        return 0.0

    found = sum(1 for e in entities if e in combined_output)
    return found / len(entities)


# ---------------------------------------------------------------------------
# Outcome reward (rubric-based judge)
# ---------------------------------------------------------------------------

RUBRIC_PROMPT = """Evaluate the troubleshooting response against these criteria.

## Question
{query}

## Model Response
{response}

## Expected Diagnosis
{expected}

## Evaluation Criteria

### ESSENTIAL (all must be true for score > 0.3):
- E1: Diagnosis is supported by tool output evidence (not hallucinated)
- E2: Response addresses the user's specific query
- E3: No contradictions with tool outputs shown in the response

### QUALITY (0.0-1.0 each):
- Q1 Efficiency: Used minimum necessary tools (fewer redundant calls = higher)
- Q2 Extraction: Pulled relevant diagnostic info from tool outputs
- Q3 Reasoning: Logical progression from symptoms to root cause

## Scoring
- If ANY essential criterion fails: score 0.0-0.3
- If all essential criteria pass: base 0.3 + average(Q1, Q2, Q3) * 0.7

Format your response as:
E1: [PASS/FAIL] [brief reason]
E2: [PASS/FAIL] [brief reason]
E3: [PASS/FAIL] [brief reason]
Q1: [0.0-1.0]
Q2: [0.0-1.0]
Q3: [0.0-1.0]
Score: [final score 0.0-1.0]"""


def score_outcome(solution_str: str, ground_truth: str, extra_info: dict | None) -> float:
    """Rubric-based judge evaluation for outcome reward.

    Uses a structured rubric with essential (blocking) and quality criteria
    to reduce exploitability compared to generic "rate correctness" prompts.
    """
    client = openai.OpenAI()
    query = extra_info.get("query", "") if extra_info else ""

    prompt = RUBRIC_PROMPT.format(
        query=query,
        response=solution_str,
        expected=ground_truth,
    )

    try:
        response = client.chat.completions.create(
            model=os.environ.get("JUDGE_MODEL", "gpt-5-mini"),
            messages=[
                {"role": "system", "content": "You are a Kubernetes troubleshooting evaluation judge. Be strict and evidence-based."},
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=2000,
        )

        text = response.choices[0].message.content or ""

        # Parse structured score
        score_match = re.search(r"Score:\s*([\d.]+)", text)
        if score_match:
            score = float(score_match.group(1))
            if score > 1.0:
                score = score / 10.0 if score <= 10.0 else score / 100.0
            return min(1.0, max(0.0, score))
    except Exception:
        pass

    return 0.0


# ---------------------------------------------------------------------------
# Running averages for PPRM normalization
# ---------------------------------------------------------------------------

class _ProcessRewardStats:
    """Track running mean of process rewards for outcome-conditioned normalization."""

    def __init__(self):
        self._sum = 0.0
        self._count = 0

    def update(self, value: float) -> None:
        self._sum += value
        self._count += 1

    @property
    def mean(self) -> float:
        return self._sum / self._count if self._count > 0 else 0.0


_stats = _ProcessRewardStats()


# ---------------------------------------------------------------------------
# Main reward function
# ---------------------------------------------------------------------------

# Component weights (must sum to ~1.0 with process + outcome)
W_INFO_GAIN = 0.25
W_RETRIEVAL = 0.15
W_UTILIZATION = 0.10
W_FORMAT = 0.05
W_REDUNDANCY = 0.05  # applied as penalty (negative)
W_TOOL_COUNT = 0.02  # applied per call (negative)
W_OUTCOME = 0.40


def compute_score(data_source, solution_str, ground_truth, extra_info=None):
    """Process-supervised reward for OLS troubleshooting.

    Combines lightweight process rewards (always on) with outcome-based judge.
    Applies PPRM outcome-conditioned normalization to prevent reward inflation.

    Args:
        data_source: dataset name (unused).
        solution_str: the model's full response text (may include tool calls/responses).
        ground_truth: the expected response from the dataset.
        extra_info: dict with additional metadata (query, scenario_id, etc.).

    Returns:
        float score, typically in range [-0.5, 1.0].
    """
    # --- Outcome reward (40%) ---
    outcome_raw = score_outcome(solution_str, ground_truth, extra_info)
    outcome_correct = outcome_raw >= 0.5

    # --- Process rewards ---

    # Always-on lightweight components
    fmt_score = score_format_compliance(solution_str)
    redundancy_score = score_redundancy(solution_str)  # negative
    tool_count_score = score_tool_count(solution_str)  # negative
    retrieval_score = score_retrieval_existence(solution_str, ground_truth)

    # Optional heavier components (controlled by env vars)
    info_gain_score = 0.0  # Requires model forward pass — disabled by default
    utilization_score = 0.0  # Requires embeddings — disabled by default

    if os.environ.get("REWARD_INFO_GAIN", "0") == "1":
        # Info gain requires a model and tokenizer — placeholder for future implementation.
        # When enabled, this would compute probability shift toward correct diagnosis
        # using teacher-forced forward pass before/after each tool call.
        pass

    if os.environ.get("REWARD_UTILIZATION", "0") == "1":
        # Utilization scoring requires embedding model — placeholder for future.
        # When enabled, this would compute embedding similarity between tool output
        # and the model's next reasoning step.
        pass

    # Weighted process reward (before normalization)
    r_process_raw = (
        W_FORMAT * fmt_score
        + W_RETRIEVAL * retrieval_score
        + W_INFO_GAIN * info_gain_score
        + W_UTILIZATION * utilization_score
        + W_REDUNDANCY * redundancy_score  # already negative
        + W_TOOL_COUNT * tool_count_score  # already negative
    )

    # Update running stats
    _stats.update(r_process_raw)
    mean_process = _stats.mean

    # CRITICAL: Outcome-conditioned normalization (PPRM)
    # Clamp process rewards positive when correct, negative when wrong.
    # Without this, training collapses from reward inflation on failed trajectories.
    if outcome_correct:
        r_process = max(0.0, r_process_raw - mean_process)
    else:
        r_process = min(0.0, r_process_raw - mean_process)

    # Total reward
    r_total = W_OUTCOME * outcome_raw + r_process

    return r_total
