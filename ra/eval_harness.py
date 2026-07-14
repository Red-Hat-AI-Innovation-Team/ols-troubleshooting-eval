"""CLI for running the offline eval harness.

Usage:
    uv run python eval_harness.py                                    # all 11 scenarios
    uv run python eval_harness.py --scenario envvar_missing           # single scenario
    uv run python eval_harness.py --model-url https://api.openai.com/v1 --model-name gpt-5.5
    uv run python eval_harness.py --judge-model claude-haiku-4-5@20251001
    uv run python eval_harness.py --output results.json
"""

import argparse
import json
import sys
from dataclasses import asdict

from eval.harness import run_all_scenarios, run_scenario
from llm import AnthropicVertexClient, OpenAIClient
from llm.config import OpenAIConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="Run offline eval harness")
    parser.add_argument(
        "--scenario",
        help="Run a single scenario by ID",
    )
    parser.add_argument(
        "--model-url",
        help="OpenAI-compatible endpoint URL for the troubleshooter model",
    )
    parser.add_argument(
        "--model-name",
        default="claude-haiku-4-5@20251001",
        help="Model name for the troubleshooter (default: claude-haiku-4-5@20251001)",
    )
    parser.add_argument(
        "--judge-model",
        default="gpt-4o-mini",
        help="Judge LLM model (default: gpt-4o-mini)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Number of parallel scenarios (default: 5)",
    )
    parser.add_argument(
        "--output",
        help="Save results to JSON file",
    )
    args = parser.parse_args()

    # Build troubleshooter client
    if args.model_url:
        troubleshooter_client = OpenAIClient(OpenAIConfig(base_url=args.model_url))
    else:
        troubleshooter_client = AnthropicVertexClient()

    # Judge always uses OpenAI client
    judge_client = OpenAIClient(OpenAIConfig())

    if args.scenario:
        print(f"Running scenario: {args.scenario}")
        result = run_scenario(
            scenario_id=args.scenario,
            troubleshooter_client=troubleshooter_client,
            troubleshooter_model=args.model_name,
            judge_client=judge_client,
            judge_model=args.judge_model,
        )
        results = {args.scenario: result}
        status = "PASS" if result.passed else "FAIL"
        print(f"\n{args.scenario}: {status} (score={result.mean_score:.2f})")
    else:
        print(f"Running all scenarios (concurrency={args.concurrency})")
        results = run_all_scenarios(
            troubleshooter_client=troubleshooter_client,
            troubleshooter_model=args.model_name,
            judge_client=judge_client,
            judge_model=args.judge_model,
            concurrency=args.concurrency,
        )

    if args.output:
        output_data = {
            sid: asdict(r) for sid, r in results.items()
        }
        with open(args.output, "w") as f:
            json.dump(output_data, f, indent=2, default=str)
        print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()
