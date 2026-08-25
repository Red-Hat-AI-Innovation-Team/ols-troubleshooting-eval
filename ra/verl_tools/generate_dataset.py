"""Generate a verl-compatible training dataset in parquet format.

Uses HuggingFace datasets to produce parquet matching verl's expected schema:
prompt as list[dict], extra_info as dict (not JSON strings).
"""

import os
import sys
from pathlib import Path

import datasets as hf_datasets

# Ensure ra/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def generate_dataset(repeats: int = 50, output: str = "ols_train.parquet"):
    from eval.scenarios import SCENARIOS, list_scenarios
    from run_agent import SYSTEM_PROMPT

    rows: list[dict] = []
    for idx, scenario_id in enumerate(list_scenarios()):
        scenario = SCENARIOS[scenario_id]
        turn = scenario.turns[0]
        for rep in range(repeats):
            # Build tools_kwargs: each tool gets scenario-specific create_kwargs
            # All tools share the same create_kwargs per rollout
            tools_kwargs = {}
            # The OLSTroubleshootingTool.create() reads scenario_id from create_kwargs
            # We set it for all tool names so any tool can init the DB
            from mock_tools import TOOLS
            for tool_name in TOOLS:
                tools_kwargs[tool_name] = {
                    "create_kwargs": {
                        "scenario_id": scenario_id,
                        "expected_response": turn.expected_response,
                    },
                }

            row = {
                "data_source": "ols/troubleshooting",
                "agent_name": "tool_agent",
                "prompt": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": turn.query},
                ],
                "reward_model": {
                    "ground_truth": turn.expected_response,
                },
                "extra_info": {
                    "split": "train",
                    "index": idx * repeats + rep,
                    "query": turn.query,
                    "scenario_id": scenario_id,
                    "expected_response": turn.expected_response,
                    "need_tools_kwargs": True,
                    "tools_kwargs": tools_kwargs,
                },
            }
            rows.append(row)

    # Use HF datasets to create parquet (handles nested dicts properly)
    ds = hf_datasets.Dataset.from_list(rows)
    ds.to_parquet(output)
    print(f"Written {len(rows)} rows to {output}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate verl training dataset")
    parser.add_argument("--repeats", type=int, default=50)
    parser.add_argument("--output", default="ols_train.parquet")
    args = parser.parse_args()
    generate_dataset(args.repeats, args.output)
