#!/usr/bin/env bash
set -euo pipefail

# Calculate overall and per-scenario metrics for each model.
#
# Usage:
#   ./eval_metrics.sh <results_dir>
#
# Example:
#   ./eval_metrics.sh eval_scenarios/results/gemma_4_e4b_jul_3
#   ./eval_metrics.sh eval_scenarios/results

RESULTS_DIR="${1:?Usage: $0 <results_dir>}"

RESULTS_DIR="$RESULTS_DIR" python3 << 'PYEOF'
import csv, glob, os, sys
from collections import defaultdict

results_dir = os.environ["RESULTS_DIR"]

traced_dirs = sorted(glob.glob(f"{results_dir}/traced_*/"))
if not traced_dirs:
    print(f"No traced_* directories found in {results_dir}")
    sys.exit(1)

models = {}
for d in traced_dirs:
    label = os.path.basename(d.rstrip("/")).replace("traced_", "")
    for sep in ("_baseline", "_its4_", "_its8_"):
        if sep in label:
            model = label[:label.index(sep)]
            config = label[label.index(sep)+1:]
            break
    else:
        model = label
        config = "unknown"
    models.setdefault(model, {})[config] = d

THRESHOLD = 0.5
CONFIG_ORDER = ["baseline", "its4_hierarchical", "its4_flat_all", "its8_hierarchical", "its8_flat_all"]
SHORT = {"baseline": "Base", "its4_hierarchical": "4-Hier", "its4_flat_all": "4-Flat",
         "its8_hierarchical": "8-Hier", "its8_flat_all": "8-Flat"}

CONV_METRICS = ["generic_troubleshooting_experience", "troubleshooting_continuity",
                "conversation_completeness", "conversation_relevancy", "knowledge_retention"]

for model, configs in sorted(models.items()):
    ordered = [c for c in CONFIG_ORDER if c in configs]
    ordered.extend(c for c in sorted(configs) if c not in CONFIG_ORDER)
    headers = [SHORT.get(c, c) for c in ordered]

    # Single pass: collect all data per config
    all_data = {}  # config -> {p, f, e, ac: {scenario: [bool]}, conv: {metric: [bool]}}
    for config_name in ordered:
        config_path = configs[config_name]
        cd = {"p": 0, "f": 0, "e": 0,
              "ac": defaultdict(list), "conv": defaultdict(list)}

        for i in range(1, 100):
            found = False
            for fp in sorted(glob.glob(f"{config_path}/iter_{i:02d}/*/*detailed*.csv")):
                found = True
                scenario = fp.split("/")[-2]
                for row in csv.DictReader(open(fp)):
                    result = row.get("result", "")
                    metric = row.get("metric_identifier", "")

                    if result == "ERROR":
                        cd["e"] += 1
                        cd["f"] += 1
                        if "answer_correctness" in metric:
                            cd["ac"][scenario].append(False)
                        for cm in CONV_METRICS:
                            if cm in metric:
                                cd["conv"][cm].append(False)
                                break
                        continue

                    try:
                        score = float(row.get("score", ""))
                    except (ValueError, TypeError):
                        continue

                    passed = score >= THRESHOLD
                    if passed:
                        cd["p"] += 1
                    else:
                        cd["f"] += 1

                    if "answer_correctness" in metric:
                        cd["ac"][scenario].append(passed)
                    for cm in CONV_METRICS:
                        if cm in metric:
                            cd["conv"][cm].append(passed)
                            break
            if not found:
                break

        all_data[config_name] = cd

    # --- Output ---
    print(f"\n{'='*90}")
    print(f"  {model}")
    print(f"{'='*90}")

    # 1. All-metrics pass rate
    fmt = "{:<25} {:>6} {:>6} {:>6} {:>6} {:>8}"
    print(f"\n  All metrics (score >= {THRESHOLD}):")
    print(f"  {fmt.format('Config', 'Pass', 'Fail', 'Error', 'Total', 'Pass%')}")
    print(f"  {'-'*62}")
    for c in ordered:
        d = all_data[c]
        total = d["p"] + d["f"]
        rate = f"{d['p']/total*100:.1f}%" if total else "n/a"
        print(f"  {fmt.format(c, d['p'], d['f'], d['e'], total, rate)}")

    # 2. Answer correctness macro-average
    def macro_avg(ac):
        per_s = [sum(v)/len(v) for v in ac.values() if v]
        return sum(per_s) / len(per_s) if per_s else 0

    ac_rates = {c: macro_avg(all_data[c]["ac"]) for c in ordered}
    best_config = max(ordered, key=lambda c: ac_rates[c])
    baseline_rate = ac_rates.get("baseline")

    fmt2 = "{:<25} {:>10}"
    print(f"\n  Answer correctness (macro-averaged, score >= {THRESHOLD}):")
    print(f"  {fmt2.format('Config', 'Pass%')}")
    print(f"  {'-'*37}")
    for c in ordered:
        marker = " <-- best" if c == best_config else ""
        print(f"  {fmt2.format(c, f'{ac_rates[c]*100:.1f}%')}{marker}")

    if baseline_rate is not None:
        delta = (ac_rates[best_config] - baseline_rate) * 100
        print(f"\n  ITS improvement: +{delta:.1f} pts (baseline {baseline_rate*100:.1f}% -> best ITS {ac_rates[best_config]*100:.1f}%, {best_config})")

    # 3. Per-scenario table
    all_scenarios = sorted(set().union(*(all_data[c]["ac"].keys() for c in ordered)))
    col_w = 8
    print(f"\n  Per-scenario answer_correctness:")
    print(f"  {'Scenario':<30}" + "".join(f"{h:>{col_w}}" for h in headers))
    print(f"  {'-' * (30 + col_w * len(ordered))}")

    scenario_rates = {c: [] for c in ordered}
    for scenario in all_scenarios:
        row = f"  {scenario:<30}"
        for c in ordered:
            samples = all_data[c]["ac"].get(scenario, [])
            if samples:
                rate = sum(samples) / len(samples)
                scenario_rates[c].append(rate)
                row += f"{f'{rate*100:.0f}%':>{col_w}}"
            else:
                row += f"{'—':>{col_w}}"
        print(row)

    print(f"  {'-' * (30 + col_w * len(ordered))}")
    avg_row = f"  {'Average':<30}"
    for c in ordered:
        vals = scenario_rates[c]
        avg_row += f"{f'{sum(vals)/len(vals)*100:.1f}%':>{col_w}}" if vals else f"{'—':>{col_w}}"
    print(avg_row)

    # 4. Conversational quality
    has_conv = any(all_data[c]["conv"] for c in ordered)
    if has_conv:
        print(f"\n  Conversational quality (wrong_networkpolicy only, score >= {THRESHOLD}):")
        print(f"  {'Metric':<40}" + "".join(f"{h:>{col_w}}" for h in headers))
        print(f"  {'-' * (40 + col_w * len(ordered))}")
        for cm in CONV_METRICS:
            row = f"  {cm:<40}"
            any_data = False
            for c in ordered:
                samples = all_data[c]["conv"].get(cm, [])
                if samples:
                    any_data = True
                    rate = sum(samples) / len(samples)
                    row += f"{f'{rate*100:.0f}%':>{col_w}}"
                else:
                    row += f"{'—':>{col_w}}"
            if any_data:
                print(row)

    print()

PYEOF
