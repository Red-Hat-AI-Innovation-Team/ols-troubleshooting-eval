"""Analyze eval failures to identify clarification-seeking behavior.

For each model, checks all iterations and scenarios for responses where the
model asks for clarification instead of using tools to investigate.

Usage:
    python3 analyze_clarification_failures.py [results_dir1] [results_dir2] ...

    If no args, analyzes all traced_* directories in the default results path.

Output:
    Per-model summary with:
    - Clarification rate (over all eval points)
    - Clarification rate given failure
    - Failure rate excluding clarification failures
    - Per-scenario breakdown
    - Example responses
"""

import csv
import glob
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field

RESULTS_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lightspeed-service", "eval", "troubleshooting", "results")

CLARIFICATION_PATTERNS = [
    r"please\s+(specify|provide|clarify|share|tell)",
    r"could you\s+(specify|provide|clarify|share|tell|confirm)",
    r"can you\s+(specify|provide|clarify|share|tell|confirm|let me know)",
    r"more\s+(context|information|details|specifics)",
    r"let me know",
    r"before i (can|proceed|investigate|diagnose)",
    r"to (help|assist|diagnose|investigate|proceed) .* (need|require|please)",
    r"in the meantime",
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in CLARIFICATION_PATTERNS]


@dataclass
class EvalPoint:
    scenario: str
    iteration: int
    result: str
    score: float
    response: str
    metric: str
    is_clarification: bool = False
    matched_patterns: list = field(default_factory=list)


def detect_clarification(response: str) -> tuple[bool, list[str]]:
    matched = []
    for pattern in COMPILED_PATTERNS:
        m = pattern.search(response.lower())
        if m:
            matched.append(m.group(0))
    return len(matched) > 0, matched


def pct(num, denom):
    return round(num / denom * 100, 1) if denom > 0 else 0.0


def analyze_model(model_dir: str) -> dict:
    model_name = os.path.basename(model_dir)
    points = []

    for iter_num in range(1, 100):
        iter_dir = os.path.join(model_dir, f"iter_{iter_num:02d}")
        if not os.path.exists(iter_dir):
            break
        for scenario_dir in sorted(glob.glob(os.path.join(iter_dir, "*"))):
            scenario = os.path.basename(scenario_dir)
            csv_files = sorted(glob.glob(os.path.join(scenario_dir, "*detailed*.csv")))
            if not csv_files:
                continue
            with open(csv_files[-1]) as fh:
                for row in csv.DictReader(fh):
                    response = row.get("response", "")
                    is_clar, matched = detect_clarification(response)
                    points.append(EvalPoint(
                        scenario=scenario,
                        iteration=iter_num,
                        result=row.get("result", ""),
                        score=float(row.get("score", 0) or 0),
                        response=response,
                        metric=row.get("metric_identifier", ""),
                        is_clarification=is_clar,
                        matched_patterns=matched,
                    ))

    total = len(points)
    passes = [p for p in points if p.result == "PASS"]
    fails = [p for p in points if p.result == "FAIL"]
    errors = [p for p in points if p.result == "ERROR"]

    all_clar = [p for p in points if p.is_clarification]
    clar_and_pass = [p for p in all_clar if p.result == "PASS"]
    clar_and_fail = [p for p in all_clar if p.result == "FAIL"]
    clar_and_error = [p for p in all_clar if p.result == "ERROR"]

    non_clar = [p for p in points if not p.is_clarification]
    non_clar_fail = [p for p in non_clar if p.result == "FAIL"]
    non_clar_error = [p for p in non_clar if p.result == "ERROR"]
    non_clar_pass = [p for p in non_clar if p.result == "PASS"]

    non_pass = [p for p in points if p.result != "PASS"]

    by_scenario = defaultdict(lambda: {
        "total": 0, "pass": 0, "fail": 0, "error": 0,
        "clar_total": 0, "clar_fail": 0, "clar_pass": 0,
    })
    for p in points:
        s = by_scenario[p.scenario]
        s["total"] += 1
        s[p.result.lower()] += 1
        if p.is_clarification:
            s["clar_total"] += 1
            if p.result == "FAIL":
                s["clar_fail"] += 1
            elif p.result == "PASS":
                s["clar_pass"] += 1

    clar_examples = defaultdict(list)
    for p in clar_and_fail:
        if len(clar_examples[p.scenario]) < 1:
            clar_examples[p.scenario].append({
                "iteration": p.iteration,
                "score": p.score,
                "metric": p.metric,
                "matched_patterns": p.matched_patterns,
                "response_preview": p.response[:300],
            })

    non_clar_fail_examples = defaultdict(list)
    for p in non_clar_fail:
        if len(non_clar_fail_examples[p.scenario]) < 1:
            non_clar_fail_examples[p.scenario].append({
                "iteration": p.iteration,
                "score": p.score,
                "metric": p.metric,
                "response_preview": p.response[:300],
            })

    return {
        "model": model_name,
        "iterations": max((p.iteration for p in points), default=0),
        "total_eval_points": total,
        "pass_count": len(passes),
        "fail_count": len(fails),
        "error_count": len(errors),

        "pass_rate": pct(len(passes), total),
        "fail_rate": pct(len(fails), total),
        "error_rate": pct(len(errors), total),

        "clarification_total": len(all_clar),
        "clarification_rate": pct(len(all_clar), total),
        "clarification_given_failure": pct(len(clar_and_fail), len(fails)),
        "clarification_pass": len(clar_and_pass),
        "clarification_fail": len(clar_and_fail),
        "clarification_error": len(clar_and_error),

        "non_clar_total": len(non_clar),
        "non_clar_pass": len(non_clar_pass),
        "non_clar_fail": len(non_clar_fail),
        "non_clar_error": len(non_clar_error),
        "non_clar_fail_rate": pct(len(non_clar_fail), len(non_clar)),
        "non_clar_pass_rate": pct(len(non_clar_pass), len(non_clar)),
        "fail_given_clar": pct(len(clar_and_fail), len(all_clar)),

        "by_scenario": dict(by_scenario),
        "clarification_examples": dict(clar_examples),
        "non_clarification_fail_examples": dict(non_clar_fail_examples),
    }


def print_report(analysis: dict):
    m = analysis
    print(f"\n{'='*80}")
    print(f"MODEL: {m['model']}")
    print(f"{'='*80}")
    print(f"Iterations: {m['iterations']}  |  Total eval points: {m['total_eval_points']}")
    print()
    print(f"OVERALL RATES:")
    print(f"  Pass rate:          {m['pass_count']:>3}/{m['total_eval_points']}  ({m['pass_rate']}%)")
    print(f"  Fail rate:          {m['fail_count']:>3}/{m['total_eval_points']}  ({m['fail_rate']}%)")
    print(f"  Error rate:         {m['error_count']:>3}/{m['total_eval_points']}  ({m['error_rate']}%)")
    print()
    print(f"CLARIFICATION RATES:")
    print(f"  Clarification rate (overall):       {m['clarification_total']:>3}/{m['total_eval_points']}  ({m['clarification_rate']}%)")
    print(f"  Clarification rate | given failure:  {m['clarification_fail']:>3}/{m['fail_count']}   ({m['clarification_given_failure']}%)")
    print(f"  Clarification -> still pass:         {m['clarification_pass']:>3}/{m['clarification_total']}   ({pct(m['clarification_pass'], m['clarification_total'])}%)")
    print(f"  Clarification -> fail:               {m['clarification_fail']:>3}/{m['clarification_total']}   ({pct(m['clarification_fail'], m['clarification_total'])}%)")
    print()
    print(f"NON-CLARIFICATION SUBSET ({m['non_clar_total']} points):")
    print(f"  Pass rate (excl. clarification):     {m['non_clar_pass']:>3}/{m['non_clar_total']}   ({m['non_clar_pass_rate']}%)")
    print(f"  Fail rate (excl. clarification):     {m['non_clar_fail']:>3}/{m['non_clar_total']}   ({m['non_clar_fail_rate']}%)")
    print(f"  Error rate (excl. clarification):    {m['non_clar_error']:>3}/{m['non_clar_total']}   ({pct(m['non_clar_error'], m['non_clar_total'])}%)")

    print(f"\nPER-SCENARIO BREAKDOWN:")
    print(f"  {'Scenario':<30} {'Tot':>3} {'Pass':>4} {'Fail':>4} {'Err':>3} {'Clar':>4} {'C/Tot':>6} {'CF/F':>6}")
    print(f"  {'-'*28}  {'---':>3} {'----':>4} {'----':>4} {'---':>3} {'----':>4} {'------':>6} {'------':>6}")
    for scenario in sorted(m["by_scenario"]):
        s = m["by_scenario"][scenario]
        c_rate = f"{s['clar_total']/s['total']*100:.0f}%" if s["total"] > 0 else "n/a"
        cf_rate = f"{s['clar_fail']/s['fail']*100:.0f}%" if s["fail"] > 0 else "n/a"
        print(f"  {scenario:<30} {s['total']:>3} {s['pass']:>4} {s['fail']:>4} {s['error']:>3} {s['clar_total']:>4} {c_rate:>6} {cf_rate:>6}")

    if m["clarification_examples"]:
        print(f"\nCLARIFICATION EXAMPLES (1 per scenario):")
        for scenario, examples in sorted(m["clarification_examples"].items()):
            for ex in examples:
                print(f"\n  [{scenario} iter_{ex['iteration']:02d}] score={ex['score']} metric={ex['metric']}")
                print(f"  Matched patterns: {ex['matched_patterns']}")
                print(f"  Response preview:")
                for line in ex["response_preview"].split("\n")[:5]:
                    print(f"    {line}")

    if m["non_clarification_fail_examples"]:
        print(f"\nNON-CLARIFICATION FAILURE EXAMPLES (1 per scenario):")
        for scenario, examples in sorted(m["non_clarification_fail_examples"].items()):
            for ex in examples:
                print(f"\n  [{scenario} iter_{ex['iteration']:02d}] score={ex['score']} metric={ex['metric']}")
                print(f"  Response preview:")
                for line in ex["response_preview"].split("\n")[:5]:
                    print(f"    {line}")


def print_cross_model_summary(all_analyses: list):
    print(f"\n{'='*80}")
    print(f"CROSS-MODEL SUMMARY")
    print(f"{'='*80}")

    header = (
        f"{'Model':<35} {'Pass%':>6} {'Clar%':>6} {'F|C':>6} {'C|F':>6} "
        f"{'NCPass%':>7} {'NCFail%':>7} {'Err%':>5}"
    )
    divider = (
        f"{'-'*33}  {'------':>6} {'------':>6} {'------':>6} {'------':>6} "
        f"{'-------':>7} {'-------':>7} {'-----':>5}"
    )
    print(header)
    print(divider)
    for m in sorted(all_analyses, key=lambda x: x["pass_rate"]):
        print(
            f"{m['model']:<35} "
            f"{m['pass_rate']:>5.1f}% "
            f"{m['clarification_rate']:>5.1f}% "
            f"{m['fail_given_clar']:>5.1f}% "
            f"{m['clarification_given_failure']:>5.1f}% "
            f"{m['non_clar_pass_rate']:>6.1f}% "
            f"{m['non_clar_fail_rate']:>6.1f}% "
            f"{m['error_rate']:>4.1f}%"
        )

    print()
    print("Legend:")
    print("  Pass%    = overall pass rate")
    print("  Clar%    = clarification rate over ALL eval points")
    print("  F|C      = P(fail | clarification) — how often clarification leads to failure")
    print("  C|F      = P(clarification | failure) — how often failures involve clarification")
    print("  NCPass%  = pass rate on non-clarification eval points only")
    print("  NCFail%  = fail rate on non-clarification eval points only")
    print("  Err%     = error rate (empty response, timeout, etc.)")


def main():
    if len(sys.argv) > 1:
        model_dirs = sys.argv[1:]
    else:
        model_dirs = sorted(glob.glob(os.path.join(RESULTS_ROOT, "traced_*")))
        model_dirs = [d for d in model_dirs if os.path.isdir(d)
                      and any(os.path.exists(os.path.join(d, f"iter_{i:02d}")) for i in range(1, 6))]

    all_analyses = []
    for model_dir in model_dirs:
        if not os.path.isdir(model_dir):
            continue
        iters = [d for d in os.listdir(model_dir) if d.startswith("iter_")]
        if len(iters) < 5:
            continue
        analysis = analyze_model(model_dir)
        all_analyses.append(analysis)
        print_report(analysis)

    if len(all_analyses) > 1:
        print_cross_model_summary(all_analyses)

    output_path = os.path.join(RESULTS_ROOT, "clarification_analysis.json")
    with open(output_path, "w") as f:
        json.dump(all_analyses, f, indent=2, default=str)
    print(f"\nFull analysis saved to: {output_path}")


if __name__ == "__main__":
    main()
