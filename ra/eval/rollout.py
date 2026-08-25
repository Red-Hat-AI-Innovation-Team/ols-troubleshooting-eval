"""RL rollout API: single and batch rollouts with SFT export."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from eval.harness import run_scenario
from llm.base import LLMClient


@dataclass
class RolloutResult:
    scenario_id: str
    conversation: list[dict]
    score: float
    query: str
    expected_response: str


def rollout(
    scenario_id: str,
    troubleshooter_client: LLMClient,
    troubleshooter_model: str,
    judge_client: LLMClient,
    judge_model: str = "gpt-5-mini",
    db_name: str | None = None,
) -> RolloutResult:
    """Single rollout for RL. Wraps run_scenario and returns simplified result."""
    result = run_scenario(
        scenario_id=scenario_id,
        troubleshooter_client=troubleshooter_client,
        troubleshooter_model=troubleshooter_model,
        judge_client=judge_client,
        judge_model=judge_model,
        db_name=db_name,
    )

    # Use the last turn's conversation and first turn's query/expected
    last_turn = result.turns[-1]
    first_turn = result.turns[0]

    return RolloutResult(
        scenario_id=scenario_id,
        conversation=last_turn.conversation,
        score=result.mean_score,
        query=first_turn.query,
        expected_response=first_turn.expected_response,
    )


def batch_rollout(
    scenario_ids: list[str],
    troubleshooter_client: LLMClient,
    troubleshooter_model: str,
    judge_client: LLMClient,
    judge_model: str = "gpt-5-mini",
    concurrency: int = 5,
) -> list[RolloutResult]:
    """Batch rollouts for RL batch collection."""
    results: list[RolloutResult] = []

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(
                rollout,
                scenario_id=sid,
                troubleshooter_client=troubleshooter_client,
                troubleshooter_model=troubleshooter_model,
                judge_client=judge_client,
                judge_model=judge_model,
            ): sid
            for sid in scenario_ids
        }

        for future in as_completed(futures):
            sid = futures[future]
            try:
                results.append(future.result())
            except Exception as e:
                print(f"  rollout {sid}: ERROR ({e})")

    return results


def export_sft_traces(
    results: list[RolloutResult],
    output_path: Path,
    min_score: float = 0.7,
) -> int:
    """Export passing rollouts as SFT data (JSONL, matching build_sft_dataset.py format).

    Returns count of exported traces.
    """
    count = 0
    with open(output_path, "w") as f:
        for r in results:
            if r.score < min_score:
                continue

            record = {
                "conversation": r.conversation,
                "scenario_id": r.scenario_id,
                "score": r.score,
                "query": r.query,
                "expected_response": r.expected_response,
            }
            f.write(json.dumps(record) + "\n")
            count += 1

    return count
