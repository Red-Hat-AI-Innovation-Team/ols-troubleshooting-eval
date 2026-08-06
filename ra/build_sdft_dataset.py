"""Build SDFT dataset from troubleshooting run traces.

Converts multi-turn run traces (processed/train/*/runs/seed_*_run_*.json) into
SDFT format: one example per assistant tool-call step (plus one for the final
text answer), where the prompt is the full conversation history up to that step
and user_response is the assistant's privileged next output.

Output format (messages-dict style, not from/value):
    {"prompt": [{"role": "system"|"user"|"assistant"|"tool", ...}],
     "user_response": {"role": "assistant", "tool_calls": [...] | "content": "..."},
     "scenario_idx": int, "seed_idx": int, "run_idx": int, "step": int}

Usage:
    uv run python build_sdft_dataset.py
    uv run python build_sdft_dataset.py --runs-dir ra/sdg/v1-opus/processed/train \
        --output ra/sdg/v1-opus/processed/train_sdft.jsonl
"""

import argparse
import json
import glob
import random
from collections import Counter, defaultdict
from pathlib import Path

SYSTEM_PROMPT = """\
You are an OpenShift/Kubernetes troubleshooting agent. Investigate cluster \
problems using the available tools and provide a clear, evidence-based \
diagnosis based on the data you gather."""

MAX_TURNS_PLACEHOLDER = "[Agent hit max turns without producing a final answer]"


def prompt_message(msg: dict) -> dict:
    """Convert a source conversation message to prompt form (ids kept, reasoning dropped)."""
    role = msg["role"]
    out: dict = {"role": role}
    if msg.get("content"):
        out["content"] = msg["content"]
    if msg.get("tool_calls"):
        out["tool_calls"] = msg["tool_calls"]
    if msg.get("tool_results"):
        out["tool_results"] = msg["tool_results"]
    return out


def target_message(msg: dict) -> dict:
    """Convert an assistant message to the target (user_response) form.

    Tool call ids are stripped — the model should not learn to generate nonce ids.
    """
    out: dict = {"role": "assistant"}
    if msg.get("tool_calls"):
        out["tool_calls"] = [
            {"name": tc["name"], "arguments": tc["arguments"]}
            for tc in msg["tool_calls"]
        ]
    if msg.get("content"):
        out["content"] = msg["content"]
    return out


def convert_run(data: dict) -> list[dict]:
    """Convert one run into a list of SDFT examples."""
    conv = data["conversation"]
    if not conv:
        return []

    prompt: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    examples: list[dict] = []
    final_answer: dict | None = None

    for i, msg in enumerate(conv):
        role = msg["role"]

        if role == "assistant" and msg.get("tool_calls"):
            target = target_message(msg)
            examples.append({
                "prompt": list(prompt),
                "user_response": target,
                "scenario_idx": data["scenario_idx"],
                "seed_idx": data["seed_idx"],
                "run_idx": data["run_idx"],
                "step": i,
            })
            prompt.append(prompt_message(msg))

        elif role == "assistant":
            if msg.get("content") and msg["content"] != MAX_TURNS_PLACEHOLDER:
                final_answer = target_message(msg)
            prompt.append(prompt_message(msg))

        else:
            prompt.append(prompt_message(msg))

    if final_answer is not None:
        examples.append({
            "prompt": list(prompt[:-1]),
            "user_response": final_answer,
            "scenario_idx": data["scenario_idx"],
            "seed_idx": data["seed_idx"],
            "run_idx": data["run_idx"],
            "step": len(conv) - 2,
        })

    return examples


def sample_run_files(run_files: list[str], n: int, rng: random.Random) -> list[str]:
    """Deterministically pick n run files from distinct (scenario, seed) groups."""
    groups: dict[tuple[int, int], list[str]] = defaultdict(list)
    for rf in run_files:
        with open(rf) as f:
            data = json.load(f)
        groups[(data["scenario_idx"], data["seed_idx"])].append(rf)

    keys = sorted(groups)
    rng.shuffle(keys)
    selected = []
    used_scenarios: set[int] = set()
    used_seeds: set[int] = set()
    for key in keys:
        if len(selected) >= n:
            break
        scenario_idx, seed_idx = key
        if scenario_idx in used_scenarios or seed_idx in used_seeds:
            continue
        used_scenarios.add(scenario_idx)
        used_seeds.add(seed_idx)
        group_runs = groups[key]
        rng.shuffle(group_runs)
        selected.append(group_runs[0])
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="Build SDFT dataset from run traces")
    parser.add_argument(
        "--runs-dir",
        default="ra/sdg/v1-opus/processed/train",
        help="Directory of scenario dirs containing runs/",
    )
    parser.add_argument(
        "--output",
        default="ra/sdg/v1-opus/processed/train_sdft.jsonl",
    )
    parser.add_argument(
        "--limit-runs",
        type=int,
        default=None,
        help="Only convert N runs, sampled from distinct scenario/seed groups",
    )
    parser.add_argument(
        "--sample-seed",
        type=int,
        default=42,
        help="RNG seed for --limit-runs sampling",
    )
    args = parser.parse_args()

    run_files = sorted(glob.glob(f"{args.runs_dir}/*/runs/seed_*_run_*.json"))
    print(f"Found {len(run_files)} run files")

    if args.limit_runs is not None:
        run_files = sample_run_files(run_files, args.limit_runs, random.Random(args.sample_seed))
        print(f"Sampled {len(run_files)} run files (sample seed {args.sample_seed}):")
        for rf in run_files:
            with open(rf) as f:
                data = json.load(f)
            print(f"  scenario {data['scenario_idx']:04d} / seed {data['seed_idx']} / run {data['run_idx']}")

    written = 0
    skipped_runs = 0
    step_dist: Counter = Counter()
    used_scenarios: set = set()

    with open(args.output, "w") as out:
        for rf in run_files:
            with open(rf) as f:
                data = json.load(f)

            examples = convert_run(data)
            if not examples:
                skipped_runs += 1
                continue

            used_scenarios.add(data["scenario_idx"])
            for ex in examples:
                out.write(json.dumps(ex) + "\n")
                step_dist[ex["step"]] += 1
            written += len(examples)

    print(f"Examples written: {written}")
    print(f"Skipped runs (no tool calls): {skipped_runs}")
    print(f"Scenarios covered: {len(used_scenarios)}")
    print("Steps per example:")
    for step in sorted(step_dist):
        print(f"  step {step}: {step_dist[step]}")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
