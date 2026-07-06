"""Split flattened seed data into test (10 seeds) and train (remaining runs).

Deterministic split via fixed random seed. Outputs:
  processed/test/seed_{0..9}.json   — 10 held-out seed data files
  processed/test/metadata.csv       — filename, scenario description
  processed/train/s{NNN}_seed{N}_run{N}.json — all runs from remaining seeds

Usage:
    uv run python split_dataset.py
    uv run python split_dataset.py --sdg-dir sdg/v1 --seed 42 --n-test 10
"""

import argparse
import csv
import json
import random
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SeedEntry:
    scenario_idx: str
    seed_idx: str
    seed_path: Path
    scenario_txt: str


def discover_seeds(sdg_dir: Path) -> list[SeedEntry]:
    """Walk sdg_dir and collect all (scenario, seed) pairs with metadata."""
    entries: list[SeedEntry] = []
    for scenario_dir in sorted(sdg_dir.iterdir()):
        if not scenario_dir.is_dir():
            continue
        scenario_txt_path = scenario_dir / "scenario.txt"
        scenario_txt = scenario_txt_path.read_text().strip() if scenario_txt_path.exists() else ""
        for seed_file in sorted(scenario_dir.glob("seed_*.json")):
            seed_idx = seed_file.stem.split("_")[1]  # "seed_3.json" -> "3"
            entries.append(SeedEntry(
                scenario_idx=scenario_dir.name,
                seed_idx=seed_idx,
                seed_path=seed_file,
                scenario_txt=scenario_txt,
            ))
    return entries


def collect_runs(sdg_dir: Path, scenario_idx: str, seed_idx: str) -> list[Path]:
    """Find all run files for a given (scenario, seed) pair."""
    runs_dir = sdg_dir / scenario_idx / "runs"
    if not runs_dir.is_dir():
        return []
    return sorted(runs_dir.glob(f"seed_{seed_idx}_run_*.json"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Split SDG dataset into test/train")
    parser.add_argument("--sdg-dir", type=Path, default=Path("sdg/v1"))
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--n-test", type=int, default=10, help="Number of seeds for test set")
    args = parser.parse_args()

    sdg_dir: Path = args.sdg_dir
    out_dir = sdg_dir / "processed"
    test_dir = out_dir / "test"
    train_dir = out_dir / "train"

    # Discover all seeds
    all_seeds = discover_seeds(sdg_dir)
    print(f"Found {len(all_seeds)} seed entries across {len(set(s.scenario_idx for s in all_seeds))} scenarios")

    # Deterministic split
    random.seed(args.seed)
    test_seeds = random.sample(all_seeds, args.n_test)
    test_keys = {(s.scenario_idx, s.seed_idx) for s in test_seeds}
    train_seeds = [s for s in all_seeds if (s.scenario_idx, s.seed_idx) not in test_keys]

    print(f"Test: {len(test_seeds)} seeds")
    print(f"Train: {len(train_seeds)} seeds")

    # Clean output dirs
    if out_dir.exists():
        shutil.rmtree(out_dir)
    test_dir.mkdir(parents=True)
    train_dir.mkdir(parents=True)

    # Write test seeds + metadata
    metadata_rows: list[dict[str, str]] = []
    for i, entry in enumerate(test_seeds):
        dst = test_dir / f"seed_{i}.json"
        shutil.copy2(entry.seed_path, dst)
        metadata_rows.append({
            "filename": dst.name,
            "scenario": entry.scenario_txt,
            "original_scenario_idx": entry.scenario_idx,
            "original_seed_idx": entry.seed_idx,
        })
        print(f"  test seed_{i}.json <- {entry.scenario_idx}/seed_{entry.seed_idx}.json")

    metadata_path = test_dir / "metadata.csv"
    with open(metadata_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["filename", "scenario", "original_scenario_idx", "original_seed_idx"])
        writer.writeheader()
        writer.writerows(metadata_rows)
    print(f"Wrote {metadata_path}")

    # Copy train runs
    train_run_count = 0
    for entry in train_seeds:
        runs = collect_runs(sdg_dir, entry.scenario_idx, entry.seed_idx)
        for run_path in runs:
            run_idx = run_path.stem.split("_")[-1]  # "seed_3_run_2.json" -> "2"
            dst_name = f"s{entry.scenario_idx}_seed{entry.seed_idx}_run{run_idx}.json"
            shutil.copy2(run_path, train_dir / dst_name)
            train_run_count += 1

    print(f"Copied {train_run_count} run files to {train_dir}")

    # Summary
    test_run_count = sum(len(collect_runs(sdg_dir, s.scenario_idx, s.seed_idx)) for s in test_seeds)
    print(f"\nSummary:")
    print(f"  Test:  {len(test_seeds)} seeds, {test_run_count} runs (held out)")
    print(f"  Train: {len(train_seeds)} seeds, {train_run_count} runs")


if __name__ == "__main__":
    main()
