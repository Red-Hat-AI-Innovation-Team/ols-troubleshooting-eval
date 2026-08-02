from __future__ import annotations

from ols_eval.models import Reproducibility, SimulationVerdict, Verdict


def generate_verdict(
    health_result: dict,
    execution_result: dict,
    topology_match: float,
    reproducibility: str,
    execution_time_ms: int,
) -> SimulationVerdict:
    passed = health_result.get("passed", False)
    checks = health_result.get("checks", [])
    before_state = health_result.get("before_state", {})
    after_state = health_result.get("after_state", {})

    repro = Reproducibility(reproducibility) if reproducibility in {r.value for r in Reproducibility} else Reproducibility.NONE

    if _is_regression(checks):
        verdict = Verdict.REGRESSION
    elif passed and repro == Reproducibility.FULL:
        verdict = Verdict.FIXED_HIGH_CONFIDENCE
    elif passed and repro in (Reproducibility.STRUCTURAL, Reproducibility.NONE):
        verdict = Verdict.FIXED_STRUCTURAL_ONLY
    elif _any_checks_pass(checks):
        verdict = Verdict.PARTIAL
    else:
        verdict = Verdict.FAILED

    logs = _collect_logs(execution_result, checks)

    return SimulationVerdict(
        verdict=verdict,
        evidence={
            "before_state": before_state,
            "after_state": after_state,
            "topology_match": topology_match,
            "logs": logs,
        },
        execution_time_ms=execution_time_ms,
        scenario_reproducibility=repro,
    )


def _is_regression(checks: list[dict]) -> bool:
    for check in checks:
        detail = check.get("detail", "")
        if "regressed:" in detail:
            return True
    return False


def _any_checks_pass(checks: list[dict]) -> bool:
    return any(c.get("passed", False) for c in checks)


def _collect_logs(execution_result: dict, checks: list[dict]) -> list[str]:
    logs: list[str] = []

    for cmd_result in execution_result.get("commands", []):
        stderr = cmd_result.get("stderr", "")
        if stderr:
            logs.append(f"cmd '{cmd_result.get('command', '')}': {stderr}")

    for check in checks:
        if not check.get("passed", False):
            logs.append(f"FAIL: {check.get('name', '')}: {check.get('detail', '')}")

    return logs
