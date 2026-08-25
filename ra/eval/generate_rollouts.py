"""Generate scored rollouts for offline GRPO.

Usage:
    uv run python -m eval.generate_rollouts \
        --model-url http://localhost:8234/v1 \
        --model-name Qwen/Qwen3-4B \
        --num-rollouts 8 \
        --output rollouts.jsonl \
        --concurrency 3
"""

import argparse
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from eval.harness import run_scenario
from eval.scenarios import list_scenarios
from llm import OpenAIClient, OpenAIConfig


def generate_rollouts(
    model_url: str,
    model_name: str,
    judge_model: str = "gpt-5-mini",
    num_rollouts: int = 8,
    concurrency: int = 3,
    scenarios: list[str] | None = None,
) -> list[dict]:
    """Generate num_rollouts rollouts per scenario.

    Returns list of dicts, each with:
    - scenario_id: str
    - rollout_idx: int
    - query: str (the eval query)
    - expected_response: str
    - response: str (model's final answer)
    - conversation: list[dict] (full message history including tool calls)
    - reward: float (judge score 0-1)
    - turn_id: str
    """
    troubleshooter = OpenAIClient(OpenAIConfig(base_url=model_url))
    judge_client = OpenAIClient(OpenAIConfig())  # uses OPENAI_API_KEY from env

    scenario_ids = scenarios or list_scenarios()
    results = []

    # Each (scenario, rollout_idx) is a work item
    work_items = [
        (sid, idx) for sid in scenario_ids for idx in range(num_rollouts)
    ]

    def _run(item):
        sid, idx = item
        db_name = f"rollout_{sid}_{idx}"
        result = run_scenario(
            scenario_id=sid,
            troubleshooter_client=troubleshooter,
            troubleshooter_model=model_name,
            judge_client=judge_client,
            judge_model=judge_model,
            db_name=db_name,
        )
        turn_results = []
        for tr in result.turns:
            turn_results.append({
                "scenario_id": sid,
                "rollout_idx": idx,
                "turn_id": tr.turn_id,
                "query": tr.query,
                "expected_response": tr.expected_response,
                "response": tr.response,
                "conversation": tr.conversation,
                "reward": tr.score,
            })
        return turn_results, result.mean_score

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_run, item): item for item in work_items}
        for future in as_completed(futures):
            sid, idx = futures[future]
            try:
                turn_results, mean_score = future.result()
                results.extend(turn_results)
                status = "PASS" if mean_score >= 0.7 else "FAIL"
                print(f"  [{sid}/{idx}] {status} (score={mean_score:.2f})")
            except Exception as e:
                print(f"  [{sid}/{idx}] ERROR: {e}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Generate GRPO rollouts")
    parser.add_argument("--model-url", required=True)
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--judge-model", default="gpt-5-mini")
    parser.add_argument("--num-rollouts", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("rollouts.jsonl"))
    parser.add_argument("--scenario", type=str, help="Single scenario only")
    args = parser.parse_args()

    scenarios = [args.scenario] if args.scenario else None

    print(f"Generating {args.num_rollouts} rollouts per scenario...")
    results = generate_rollouts(
        model_url=args.model_url,
        model_name=args.model_name,
        judge_model=args.judge_model,
        num_rollouts=args.num_rollouts,
        concurrency=args.concurrency,
        scenarios=scenarios,
    )

    with open(args.output, "w") as f:
        for r in results:
            f.write(json.dumps(r, default=str) + "\n")

    by_scenario: dict[str, list[float]] = defaultdict(list)
    for r in results:
        by_scenario[r["scenario_id"]].append(r["reward"])

    print(f"\nSaved {len(results)} rollouts to {args.output}")
    print(f"\n{'SCENARIO':<35} {'MEAN':>8} {'MIN':>8} {'MAX':>8}")
    print("-" * 60)
    for sid in sorted(by_scenario):
        rewards = by_scenario[sid]
        print(
            f"{sid:<35} "
            f"{sum(rewards) / len(rewards):>8.2f} "
            f"{min(rewards):>8.2f} "
            f"{max(rewards):>8.2f}"
        )


if __name__ == "__main__":
    main()
