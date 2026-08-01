from __future__ import annotations

import pytest

from simulate_mcp.models import Reproducibility, Verdict
from simulate_mcp.verdict import generate_verdict


def _health_passed(before_status="CrashLoopBackOff", after_status="Running"):
    return {
        "passed": True,
        "before_state": {"ns": [{"name": "app", "status": before_status, "ready": False, "restart_count": 0}]},
        "after_state": {"ns": [{"name": "app", "status": after_status, "ready": True, "restart_count": 0}]},
        "checks": [
            {"name": "ns/app: status transition", "passed": True, "detail": f"{before_status} -> {after_status}"},
            {"name": "ns/app: readiness", "passed": True, "detail": "ready=True"},
            {"name": "ns/app: restart stability", "passed": True, "detail": "delta=0"},
        ],
    }


def _health_failed():
    return {
        "passed": False,
        "before_state": {"ns": [{"name": "app", "status": "CrashLoopBackOff", "ready": False, "restart_count": 5}]},
        "after_state": {"ns": [{"name": "app", "status": "CrashLoopBackOff", "ready": False, "restart_count": 8}]},
        "checks": [
            {"name": "ns/app: status transition", "passed": False, "detail": "still in bad state: CrashLoopBackOff"},
            {"name": "ns/app: readiness", "passed": False, "detail": "ready=False"},
            {"name": "ns/app: restart stability", "passed": False, "detail": "restarts: 5 -> 8 (delta=3)"},
        ],
    }


def _health_partial():
    return {
        "passed": False,
        "before_state": {"ns": [{"name": "app", "status": "CrashLoopBackOff", "ready": False, "restart_count": 5}]},
        "after_state": {"ns": [{"name": "app", "status": "Running", "ready": False, "restart_count": 5}]},
        "checks": [
            {"name": "ns/app: status transition", "passed": True, "detail": "CrashLoopBackOff -> Running"},
            {"name": "ns/app: readiness", "passed": False, "detail": "ready=False"},
            {"name": "ns/app: restart stability", "passed": True, "detail": "delta=0"},
        ],
    }


def _health_regression():
    return {
        "passed": False,
        "before_state": {"ns": [{"name": "app", "status": "Running", "ready": True, "restart_count": 0}]},
        "after_state": {"ns": [{"name": "app", "status": "CrashLoopBackOff", "ready": False, "restart_count": 3}]},
        "checks": [
            {"name": "ns/app: status transition", "passed": False, "detail": "regressed: Running -> CrashLoopBackOff"},
            {"name": "ns/app: readiness", "passed": False, "detail": "ready=False"},
            {"name": "ns/app: restart stability", "passed": False, "detail": "restarts: 0 -> 3 (delta=3)"},
        ],
    }


def _exec_result(commands=None):
    return {
        "commands": commands or [],
        "manifests_applied": 1,
        "manifests_failed": 0,
    }


class TestFixedHighConfidence:
    def test_all_pass_full_repro(self):
        v = generate_verdict(
            health_result=_health_passed(),
            execution_result=_exec_result(),
            topology_match=1.0,
            reproducibility="full",
            execution_time_ms=5000,
        )
        assert v.verdict == Verdict.FIXED_HIGH_CONFIDENCE
        assert v.scenario_reproducibility == Reproducibility.FULL
        assert v.execution_time_ms == 5000
        assert v.evidence["topology_match"] == 1.0


class TestFixedStructuralOnly:
    def test_all_pass_structural_repro(self):
        v = generate_verdict(
            health_result=_health_passed(),
            execution_result=_exec_result(),
            topology_match=0.8,
            reproducibility="structural",
            execution_time_ms=10000,
        )
        assert v.verdict == Verdict.FIXED_STRUCTURAL_ONLY

    def test_all_pass_none_repro(self):
        v = generate_verdict(
            health_result=_health_passed(),
            execution_result=_exec_result(),
            topology_match=0.5,
            reproducibility="none",
            execution_time_ms=3000,
        )
        assert v.verdict == Verdict.FIXED_STRUCTURAL_ONLY


class TestPartial:
    def test_some_checks_pass(self):
        v = generate_verdict(
            health_result=_health_partial(),
            execution_result=_exec_result(),
            topology_match=0.7,
            reproducibility="full",
            execution_time_ms=8000,
        )
        assert v.verdict == Verdict.PARTIAL


class TestFailed:
    def test_no_improvement(self):
        v = generate_verdict(
            health_result=_health_failed(),
            execution_result=_exec_result(),
            topology_match=0.5,
            reproducibility="full",
            execution_time_ms=6000,
        )
        assert v.verdict == Verdict.FAILED


class TestRegression:
    def test_after_state_worse(self):
        v = generate_verdict(
            health_result=_health_regression(),
            execution_result=_exec_result(),
            topology_match=1.0,
            reproducibility="full",
            execution_time_ms=4000,
        )
        assert v.verdict == Verdict.REGRESSION


class TestEvidence:
    def test_before_after_state_in_evidence(self):
        v = generate_verdict(
            health_result=_health_passed(),
            execution_result=_exec_result(),
            topology_match=0.9,
            reproducibility="full",
            execution_time_ms=5000,
        )
        assert "before_state" in v.evidence
        assert "after_state" in v.evidence
        assert v.evidence["topology_match"] == 0.9

    def test_logs_include_stderr(self):
        exec_result = _exec_result(commands=[
            {"command": "kubectl apply -f fix.yaml", "stdout": "", "stderr": "warning: resource exists", "exit_code": 0}
        ])
        v = generate_verdict(
            health_result=_health_passed(),
            execution_result=exec_result,
            topology_match=1.0,
            reproducibility="full",
            execution_time_ms=5000,
        )
        assert any("warning" in log for log in v.evidence["logs"])

    def test_logs_include_failed_checks(self):
        v = generate_verdict(
            health_result=_health_failed(),
            execution_result=_exec_result(),
            topology_match=0.5,
            reproducibility="full",
            execution_time_ms=6000,
        )
        assert any("FAIL" in log for log in v.evidence["logs"])


class TestInvalidReproducibility:
    def test_unknown_repro_defaults_to_none(self):
        v = generate_verdict(
            health_result=_health_passed(),
            execution_result=_exec_result(),
            topology_match=1.0,
            reproducibility="unknown_value",
            execution_time_ms=5000,
        )
        assert v.scenario_reproducibility == Reproducibility.NONE
        assert v.verdict == Verdict.FIXED_STRUCTURAL_ONLY
