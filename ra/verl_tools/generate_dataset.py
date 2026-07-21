"""Generate a verl-compatible training dataset in parquet format.

verl expects parquet with columns: data_source, prompt, reward_model, extra_info.
Each row is one scenario instance (repeated ``--repeats`` times for training diversity).
"""

import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

# Ensure ra/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def generate_dataset(repeats: int = 50, output: str = "ols_train.parquet"):
    from eval.scenarios import SCENARIOS
    from run_agent import SYSTEM_PROMPT

    rows: list[dict] = []
    for scenario_id, scenario in SCENARIOS.items():
        turn = scenario.turns[0]
        for _ in range(repeats):
            row = {
                "data_source": "ols/troubleshooting",
                "prompt": json.dumps([
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": turn.query},
                ]),
                "reward_model": json.dumps({
                    "ground_truth": turn.expected_response,
                }),
                "extra_info": json.dumps({
                    "need_tools_kwargs": True,
                    "query": turn.query,
                    "scenario_id": scenario_id,
                    "expected_response": turn.expected_response,
                    "tools_kwargs": {},
                }),
            }
            rows.append(row)

    table = pa.table({
        "data_source": [r["data_source"] for r in rows],
        "prompt": [r["prompt"] for r in rows],
        "reward_model": [r["reward_model"] for r in rows],
        "extra_info": [r["extra_info"] for r in rows],
    })
    pq.write_table(table, output)
    print(f"Written {len(rows)} rows to {output}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate verl training dataset")
    parser.add_argument("--repeats", type=int, default=50)
    parser.add_argument("--output", default="ols_train.parquet")
    args = parser.parse_args()
    generate_dataset(args.repeats, args.output)
