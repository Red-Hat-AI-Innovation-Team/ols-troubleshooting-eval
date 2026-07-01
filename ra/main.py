"""Two-stage pipeline: generate seed data per scenario, then run agent evaluations.

Stage 1 — Seed data generation:
    For each scenario in scenarios.txt, generate N_SEEDS seed data variants.
    Each is cached to disk immediately after generation.

Stage 2 — Agent runs:
    For each scenario × seed × run, execute the troubleshooting agent.
    Each conversation is cached to disk immediately after completion.

Usage:
    uv run python main.py                          # run both stages
    uv run python main.py --stage seed             # seed generation only
    uv run python main.py --stage run              # agent runs only (seeds must exist)
    uv run python main.py --seeds 3 --runs 2       # override counts
"""

import argparse
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psycopg2.errors

import db
from generate_scenario_based_data import generate_seed_data
from llm import AnthropicVertexClient
from llm.config.anthropic_vertex import AnthropicVertexConfig
from run_agent import run

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCENARIOS_PATH = Path(__file__).parent / "scenarios.txt"
OUTPUT_DIR = Path(__file__).parent / "sdg" / "v1"

N_SEEDS = 5
N_RUNS = 5


def load_scenarios() -> list[str]:
    lines = SCENARIOS_PATH.read_text().strip().splitlines()
    return [line.strip() for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# Stage 1: Seed data generation
# ---------------------------------------------------------------------------


def stage_seed(scenarios: list[str], n_seeds: int) -> None:
    print(f"\n{'=' * 70}")
    print(f"STAGE 1: SEED DATA GENERATION ({len(scenarios)} scenarios × {n_seeds} seeds)")
    print(f"{'=' * 70}\n")

    # init empty DB for schema introspection
    db.init_db({})
    config = AnthropicVertexConfig(max_concurrency=50)
    llm_client = AnthropicVertexClient(config)

    # Build flat work list, pre-create dirs + scenario.txt
    work_items: list[tuple[int, str, int, Path]] = []
    for sc_idx, scenario in enumerate(scenarios):
        sc_dir = OUTPUT_DIR / f"{sc_idx:04d}"
        sc_dir.mkdir(parents=True, exist_ok=True)

        scenario_path = sc_dir / "scenario.txt"
        if not scenario_path.exists():
            scenario_path.write_text(scenario)

        for seed_idx in range(n_seeds):
            seed_path = sc_dir / f"seed_{seed_idx}.json"
            if seed_path.exists():
                print(f"[{sc_idx}/{seed_idx}] cached: {seed_path}")
                continue
            work_items.append((sc_idx, scenario, seed_idx, seed_path))

    print(f"\n{len(work_items)} seed(s) to generate (pool size: {config.max_concurrency})\n")

    def _generate(item: tuple[int, str, int, Path]) -> str:
        sc_idx, scenario, seed_idx, seed_path = item
        print(f"[{sc_idx}/{seed_idx}] generating seed data...")
        seed_data = generate_seed_data(scenario, llm_client)
        seed_path.write_text(json.dumps(seed_data, indent=2))
        return f"[{sc_idx}/{seed_idx}] -> saved: {seed_path}"

    with ThreadPoolExecutor(max_workers=config.max_concurrency) as pool:
        futures = [pool.submit(_generate, item) for item in work_items]
        for future in as_completed(futures):
            print(future.result())

    db.teardown_db()


# ---------------------------------------------------------------------------
# Stage 2: Agent runs
# ---------------------------------------------------------------------------


def stage_run(scenarios: list[str], n_seeds: int, n_runs: int) -> None:
    print(f"\n{'=' * 70}")
    print(f"STAGE 2: AGENT RUNS ({len(scenarios)} scenarios × {n_seeds} seeds × {n_runs} runs)")
    print(f"{'=' * 70}\n")

    config = AnthropicVertexConfig(max_concurrency=50)
    llm_client = AnthropicVertexClient(config)

    # Build flat work list
    work_items: list[tuple[int, str, int, dict, int, Path]] = []
    for sc_idx, scenario in enumerate(scenarios):
        sc_dir = OUTPUT_DIR / f"{sc_idx:04d}"
        runs_dir = sc_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        for seed_idx in range(n_seeds):
            seed_path = sc_dir / f"seed_{seed_idx}.json"
            if not seed_path.exists():
                print(f"[{sc_idx}/{seed_idx}] SKIP — seed not generated yet")
                continue

            seed_data = json.loads(seed_path.read_text())

            for run_idx in range(n_runs):
                run_path = runs_dir / f"seed_{seed_idx}_run_{run_idx}.json"
                if run_path.exists():
                    print(f"[{sc_idx}/{seed_idx}/{run_idx}] cached: {run_path}")
                    continue
                work_items.append((sc_idx, scenario, seed_idx, seed_data, run_idx, run_path))

    print(f"\n{len(work_items)} run(s) to execute (pool size: {config.max_concurrency})\n")

    def _run_agent(item: tuple[int, str, int, dict, int, Path]) -> str:
        sc_idx, scenario, seed_idx, seed_data, run_idx, run_path = item
        db_name = f"ols_run_{sc_idx}_{seed_idx}_{run_idx}"
        print(f"[{sc_idx}/{seed_idx}/{run_idx}] running agent (db: {db_name})...")

        try:
            db.init_db(seed_data, db_name=db_name)
        except psycopg2.errors.InvalidTextRepresentation:
            db.teardown_db(db_name=db_name)
            seed_path = OUTPUT_DIR / f"{sc_idx:04d}" / f"seed_{seed_idx}.json"
            seed_path.unlink(missing_ok=True)
            return f"[{sc_idx}/{seed_idx}/{run_idx}] BAD SEED DATA — deleted {seed_path}, will regenerate next run"

        conversation = run(seed_data, db_name=db_name, client=llm_client)
        db.teardown_db(db_name=db_name)

        result = {
            "scenario_idx": sc_idx,
            "seed_idx": seed_idx,
            "run_idx": run_idx,
            "run_id": str(uuid.uuid4()),
            "scenario": scenario,
            "conversation": conversation,
        }

        run_path.write_text(json.dumps(result, indent=2))
        return f"[{sc_idx}/{seed_idx}/{run_idx}] -> saved: {run_path}"

    with ThreadPoolExecutor(max_workers=config.max_concurrency) as pool:
        futures = [pool.submit(_run_agent, item) for item in work_items]
        for future in as_completed(futures):
            print(future.result())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Scenario seed generation + agent evaluation pipeline")
    parser.add_argument("--stage", choices=["seed", "run", "both"], default="both")
    parser.add_argument("--seeds", type=int, default=N_SEEDS, help=f"Seeds per scenario (default: {N_SEEDS})")
    parser.add_argument("--runs", type=int, default=N_RUNS, help=f"Runs per seed (default: {N_RUNS})")
    args = parser.parse_args()

    scenarios = load_scenarios()
    print(f"Loaded {len(scenarios)} scenarios from {SCENARIOS_PATH.name}")

    if args.stage in ("seed", "both"):
        stage_seed(scenarios, args.seeds)

    if args.stage in ("run", "both"):
        stage_run(scenarios, args.seeds, args.runs)

    print("\nDone.")


if __name__ == "__main__":
    main()
