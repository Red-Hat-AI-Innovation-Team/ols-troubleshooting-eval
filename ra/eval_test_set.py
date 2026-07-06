"""Evaluate the troubleshooting agent on the held-out test set.

For each test seed, runs N attempts:
  1. User simulator generates an initial question (from seed's "god knowledge")
  2. Troubleshooter investigates with tools (up to 20 internal tool-calling turns)
  3. Evaluator judges the answer against reference root causes + cluster DB → pass/fail + reason

The evaluator gets:
  - Reference root causes extracted from prior multi-turn runs (reference_root_causes.json)
  - The full cluster DB (seed data) to verify specific names/details

Usage:
    uv run python eval_test_set.py
    uv run python eval_test_set.py --seed-file sdg/v1/processed/test/seed_0.json --runs 1
    uv run python eval_test_set.py --runs 3 --concurrency 20
"""

import argparse
import csv
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import db
from agent import Agent
from llm import AnthropicVertexClient
from llm.config.anthropic_vertex import AnthropicVertexConfig
import mock_tools
from run_agent import (
    INITIAL_QUESTION_PROMPT,
    SYSTEM_PROMPT,
    USER_SIM_SYSTEM_PROMPT,
)


N_RUNS = 5

EVALUATOR_SYSTEM_PROMPT = """\
You are an expert evaluator for an AI troubleshooting agent.

You have two sources of ground truth:

1. REFERENCE ROOT CAUSES from prior validated runs (multiple independent diagnoses \
of the same cluster issue):
{reference_root_causes}

2. The full CLUSTER DATABASE backing this cluster — every table, every row. Use \
this to verify specific names, values, and details mentioned in the agent's response:
{seed_data}

You will receive a troubleshooting question and the agent's response. Your job is \
to judge whether the agent correctly identified the root cause.

Respond with ONLY a JSON object (no markdown, no code fences):
{{"pass": true/false, "reason": "1-3 sentence explanation of why it passed or failed"}}

Criteria for PASS:
- The agent identified the correct root cause consistent with the reference diagnoses
- Details (resource names, namespaces, error messages) are consistent with the cluster DB
- Minor omissions or imprecise wording are acceptable if the core diagnosis is right
- The agent does NOT need to find every single detail

Criteria for FAIL:
- The agent missed the root cause entirely
- The agent identified a symptom but not the underlying cause
- The agent fabricated details that contradict the cluster DB
- The agent gave generic advice without investigating
- The diagnosis contradicts the reference root causes"""

EVALUATOR_JUDGE_PROMPT = """\
QUESTION FROM SRE:
{question}

AGENT'S RESPONSE:
{answer}

Judge whether the agent correctly identified the root cause. Respond with ONLY \
a JSON object: {{"pass": true/false, "reason": "..."}}"""


@dataclass
class EvalResult:
    seed_idx: int
    run_idx: int
    passed: bool
    reason: str
    question: str
    answer: str
    run_id: str


def load_metadata(test_dir: Path) -> dict[int, str]:
    """Load metadata.csv and return {seed_idx: scenario_txt}."""
    metadata_path = test_dir / "metadata.csv"
    result: dict[int, str] = {}
    with open(metadata_path) as f:
        for row in csv.DictReader(f):
            seed_idx = int(row["filename"].removesuffix(".json").split("_")[1])
            result[seed_idx] = row["scenario"]
    return result


def load_reference_root_causes(test_dir: Path) -> dict[int, list[str]]:
    """Load reference_root_causes.json and return {seed_idx: [answer1, answer2, ...]}."""
    ref_path = test_dir / "reference_root_causes.json"
    raw: dict[str, list[str]] = json.loads(ref_path.read_text())
    result: dict[int, list[str]] = {}
    for filename, answers in raw.items():
        seed_idx = int(filename.removesuffix(".json").split("_")[1])
        result[seed_idx] = answers
    return result


def format_reference_answers(answers: list[str]) -> str:
    """Format reference root causes for the evaluator prompt."""
    parts: list[str] = []
    for i, answer in enumerate(answers, 1):
        parts.append(f"--- Reference diagnosis {i} ---\n{answer}")
    return "\n\n".join(parts)


def run_single_eval(
    seed_data: dict[str, list[dict]],
    reference_answers: list[str],
    client: AnthropicVertexClient,
    db_name: str,
) -> tuple[bool, str, str, str]:
    """Run a single-round eval. Returns (passed, reason, question, answer)."""
    dsn = db._dsn_for(db_name)
    seed_data_str = json.dumps(seed_data, indent=2)

    with db.connect(dsn) as conn:
        # User sim generates the initial question
        user_sim = Agent(
            system_prompt=USER_SIM_SYSTEM_PROMPT.format(seed_data=seed_data_str),
            model="claude-opus-4-6@default",
            tool_defs=[],
            tool_handler=lambda _name, _params: "",
            client=client,
            max_turns=1,
            thinking_budget=5_000,
            max_tokens=8_000,
        )

        troubleshooter = Agent(
            system_prompt=SYSTEM_PROMPT,
            model="claude-haiku-4-5@20251001",
            tool_defs=mock_tools.load_tool_defs(),
            tool_handler=mock_tools.make_tool_handler(conn),
            client=client,
        )

        question = user_sim.run(INITIAL_QUESTION_PROMPT)
        answer = troubleshooter.run(question)

    # Evaluator judges against reference root causes + cluster DB
    evaluator = Agent(
        system_prompt=EVALUATOR_SYSTEM_PROMPT.format(
            reference_root_causes=format_reference_answers(reference_answers),
            seed_data=seed_data_str,
        ),
        model="claude-opus-4-6@default",
        tool_defs=[],
        tool_handler=lambda _name, _params: "",
        client=client,
        max_turns=1,
        thinking_budget=5_000,
        max_tokens=8_000,
    )

    judge_input = EVALUATOR_JUDGE_PROMPT.format(question=question, answer=answer)
    verdict_raw = evaluator.run(judge_input)

    # Parse verdict
    verdict_raw_clean = verdict_raw.strip()
    if verdict_raw_clean.startswith("```"):
        verdict_raw_clean = verdict_raw_clean.strip("`").removeprefix("json").strip()
    verdict = json.loads(verdict_raw_clean)
    passed = bool(verdict["pass"])
    reason = verdict["reason"]

    return passed, reason, question, answer


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate agent on held-out test set")
    parser.add_argument("--test-dir", type=Path, default=Path("sdg/v1/processed/test"))
    parser.add_argument("--seed-file", type=Path, help="Run on a single seed file instead of all")
    parser.add_argument("--runs", type=int, default=N_RUNS, help=f"Runs per seed (default: {N_RUNS})")
    parser.add_argument("--output", type=Path, default=Path("eval_results.json"))
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args()

    # Load metadata and reference root causes
    test_dir: Path = args.test_dir
    if args.seed_file:
        test_dir = args.seed_file.parent
    scenario_map = load_metadata(test_dir)
    reference_map = load_reference_root_causes(test_dir)

    if args.seed_file:
        seed_files = [args.seed_file]
    else:
        seed_files = sorted(test_dir.glob("seed_*.json"))
    print(f"Found {len(seed_files)} test seeds")

    config = AnthropicVertexConfig(max_concurrency=args.concurrency)
    client = AnthropicVertexClient(config)

    # Build work items: (seed_idx, run_idx, seed_data, reference_answers)
    work_items: list[tuple[int, int, dict, list[str]]] = []
    for seed_file in seed_files:
        seed_idx = int(seed_file.stem.split("_")[1])
        seed_data = json.loads(seed_file.read_text())
        reference_answers = reference_map[seed_idx]
        for run_idx in range(args.runs):
            work_items.append((seed_idx, run_idx, seed_data, reference_answers))

    print(f"Total attempts: {len(work_items)} ({len(seed_files)} seeds x {args.runs} runs)")
    print(f"Concurrency: {args.concurrency}\n")

    results: list[EvalResult] = []

    def _run(item: tuple[int, int, dict, list[str]]) -> EvalResult:
        seed_idx, run_idx, seed_data, reference_answers = item
        db_name = f"ols_eval_{seed_idx}_{run_idx}"
        print(f"[seed_{seed_idx}/run_{run_idx}] starting...")

        db.init_db(seed_data, db_name=db_name)
        passed, reason, question, answer = run_single_eval(
            seed_data, reference_answers, client, db_name,
        )
        db.teardown_db(db_name=db_name)

        status = "PASS" if passed else "FAIL"
        print(f"[seed_{seed_idx}/run_{run_idx}] {status}: {reason}")

        return EvalResult(
            seed_idx=seed_idx,
            run_idx=run_idx,
            passed=passed,
            reason=reason,
            question=question,
            answer=answer,
            run_id=str(uuid.uuid4()),
        )

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(_run, item): item for item in work_items}
        for future in as_completed(futures):
            seed_idx, run_idx, _, _ = futures[future]
            result = future.result()
            results.append(result)

    # --- Results ---
    results.sort(key=lambda r: (r.seed_idx, r.run_idx))

    total = len(results)
    passed_count = sum(1 for r in results if r.passed)

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed_count}/{total} passed ({100 * passed_count / total:.1f}%)")
    print(f"{'=' * 60}\n")

    # Per-seed breakdown
    print(f"{'Seed':<8} {'Pass/Total':<12} {'Rate':>6}")
    print("-" * 28)
    for seed_file in seed_files:
        seed_idx = int(seed_file.stem.split("_")[1])
        seed_results = [r for r in results if r.seed_idx == seed_idx]
        seed_passed = sum(1 for r in seed_results if r.passed)
        rate = 100 * seed_passed / len(seed_results) if seed_results else 0
        print(f"seed_{seed_idx:<3} {seed_passed}/{len(seed_results):<10} {rate:5.1f}%")

    # Save detailed results
    output_data = {
        "summary": {
            "total": total,
            "passed": passed_count,
            "rate": passed_count / total if total else 0,
            "runs_per_seed": args.runs,
        },
        "results": [
            {
                "seed_idx": r.seed_idx,
                "run_idx": r.run_idx,
                "run_id": r.run_id,
                "passed": r.passed,
                "reason": r.reason,
                "question": r.question,
                "answer": r.answer,
            }
            for r in results
        ],
    }
    args.output.write_text(json.dumps(output_data, indent=2))
    print(f"\nDetailed results saved to {args.output}")


if __name__ == "__main__":
    main()
