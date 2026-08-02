from __future__ import annotations

from unittest.mock import patch

import pytest

import ols_eval.cluster
import ols_eval.executor
import ols_eval.healthcheck
import ols_eval.sanitize
import ols_eval.snapshot
from ols_eval.models import ProposedFix, Verdict
from ols_eval.pipeline import run_pipeline


def _correct_fix() -> ProposedFix:
    return ProposedFix(
        commands=[
            "kubectl set env deploy/order-fulfillment-daemon DEPLOY_ENV=production -n warehouse-ops",
        ],
        manifests=[],
        fault_description="DEPLOY_ENV environment variable undefined",
    )


def _incorrect_fix() -> ProposedFix:
    return ProposedFix(
        commands=[
            "kubectl set env deploy/order-fulfillment-daemon WRONG_VAR=test -n warehouse-ops",
        ],
        manifests=[],
        fault_description="DEPLOY_ENV environment variable undefined",
    )


def _mock_health_passed() -> dict:
    return {
        "passed": True,
        "before_state": {
            "warehouse-ops": [
                {"name": "order-fulfillment-daemon-abc", "status": "CrashLoopBackOff", "ready": False, "restart_count": 5},
            ],
        },
        "after_state": {
            "warehouse-ops": [
                {"name": "order-fulfillment-daemon-abc", "status": "Running", "ready": True, "restart_count": 5},
            ],
        },
        "checks": [
            {"name": "warehouse-ops/order-fulfillment-daemon-abc: status transition", "passed": True, "detail": "CrashLoopBackOff -> Running"},
            {"name": "warehouse-ops/order-fulfillment-daemon-abc: readiness", "passed": True, "detail": "ready=True"},
            {"name": "warehouse-ops/order-fulfillment-daemon-abc: restart stability", "passed": True, "detail": "restarts: 5 -> 5 (delta=0)"},
        ],
    }


def _mock_health_failed() -> dict:
    return {
        "passed": False,
        "before_state": {
            "warehouse-ops": [
                {"name": "order-fulfillment-daemon-abc", "status": "CrashLoopBackOff", "ready": False, "restart_count": 5},
            ],
        },
        "after_state": {
            "warehouse-ops": [
                {"name": "order-fulfillment-daemon-abc", "status": "CrashLoopBackOff", "ready": False, "restart_count": 8},
            ],
        },
        "checks": [
            {"name": "warehouse-ops/order-fulfillment-daemon-abc: status transition", "passed": False, "detail": "still in bad state: CrashLoopBackOff"},
            {"name": "warehouse-ops/order-fulfillment-daemon-abc: readiness", "passed": False, "detail": "ready=False"},
            {"name": "warehouse-ops/order-fulfillment-daemon-abc: restart stability", "passed": False, "detail": "restarts: 5 -> 8 (delta=3)"},
        ],
    }


@pytest.mark.integration
class TestEnvvarMissingCorrectFix:
    @patch("ols_eval.healthcheck.verify")
    @patch("ols_eval.healthcheck.capture_pod_states")
    @patch("ols_eval.executor.execute_fix")
    @patch("ols_eval.cluster.apply_snapshot")
    @patch("ols_eval.cluster.provision")
    @patch("ols_eval.cluster.teardown")
    @patch("ols_eval.pipeline.emit_phase_event")
    def test_correct_fix_yields_fixed_verdict(
        self, _emit, mock_teardown, mock_provision, mock_apply, mock_exec, mock_capture, mock_verify,
    ):
        mock_provision.return_value = "/tmp/test.kubeconfig"
        mock_apply.return_value = {"applied": 1, "failed": 0, "errors": []}
        mock_teardown.return_value = None
        mock_capture.return_value = _mock_health_passed()["before_state"]
        mock_verify.return_value = _mock_health_passed()

        mock_exec.return_value = {
            "commands": [
                {"command": "kubectl set env deploy/order-fulfillment-daemon DEPLOY_ENV=production -n warehouse-ops", "stdout": "", "stderr": "", "exit_code": 0},
            ],
            "manifests_applied": 0,
            "manifests_failed": 0,
        }

        verdict = run_pipeline(
            proposed_fix=_correct_fix(),
            target_namespaces=["warehouse-ops"],
            reproducibility="full",
        )

        assert verdict.verdict in (Verdict.FIXED_HIGH_CONFIDENCE, Verdict.FIXED_STRUCTURAL_ONLY)
        assert verdict.verdict != Verdict.FAILED
        assert verdict.verdict != Verdict.REGRESSION
        mock_provision.assert_called_once()
        mock_teardown.assert_called_once()


@pytest.mark.integration
class TestEnvvarMissingIncorrectFix:
    @patch("ols_eval.healthcheck.verify")
    @patch("ols_eval.healthcheck.capture_pod_states")
    @patch("ols_eval.executor.execute_fix")
    @patch("ols_eval.cluster.apply_snapshot")
    @patch("ols_eval.cluster.provision")
    @patch("ols_eval.cluster.teardown")
    @patch("ols_eval.pipeline.emit_phase_event")
    def test_incorrect_fix_yields_failed_verdict(
        self, _emit, mock_teardown, mock_provision, mock_apply, mock_exec, mock_capture, mock_verify,
    ):
        mock_provision.return_value = "/tmp/test.kubeconfig"
        mock_apply.return_value = {"applied": 1, "failed": 0, "errors": []}
        mock_teardown.return_value = None
        mock_capture.return_value = _mock_health_failed()["before_state"]
        mock_verify.return_value = _mock_health_failed()

        mock_exec.return_value = {
            "commands": [
                {"command": "kubectl set env deploy/order-fulfillment-daemon WRONG_VAR=test -n warehouse-ops", "stdout": "", "stderr": "", "exit_code": 0},
            ],
            "manifests_applied": 0,
            "manifests_failed": 0,
        }

        verdict = run_pipeline(
            proposed_fix=_incorrect_fix(),
            target_namespaces=["warehouse-ops"],
            reproducibility="full",
        )

        assert verdict.verdict == Verdict.FAILED
        mock_teardown.assert_called_once()


@pytest.mark.integration
class TestEnvvarMissingProvisionFailure:
    @patch("ols_eval.cluster.provision")
    @patch("ols_eval.cluster.teardown")
    @patch("ols_eval.pipeline.emit_phase_event")
    def test_provision_failure_returns_failed_verdict(
        self, _emit, mock_teardown, mock_provision,
    ):
        mock_provision.side_effect = RuntimeError("k3d not available")
        mock_teardown.return_value = None

        verdict = run_pipeline(
            proposed_fix=_correct_fix(),
            target_namespaces=["warehouse-ops"],
            reproducibility="full",
        )

        assert verdict.verdict == Verdict.FAILED
        assert any("k3d not available" in log for log in verdict.evidence.get("logs", []))
        mock_teardown.assert_called_once()


@pytest.mark.integration
class TestEnvvarMissingTeardownAlwaysRuns:
    @patch("ols_eval.healthcheck.capture_pod_states")
    @patch("ols_eval.executor.execute_fix")
    @patch("ols_eval.cluster.apply_snapshot")
    @patch("ols_eval.cluster.provision")
    @patch("ols_eval.cluster.teardown")
    @patch("ols_eval.pipeline.emit_phase_event")
    def test_teardown_runs_even_on_executor_error(
        self, _emit, mock_teardown, mock_provision, mock_apply, mock_exec, mock_capture,
    ):
        mock_provision.return_value = "/tmp/test.kubeconfig"
        mock_apply.return_value = {"applied": 1, "failed": 0, "errors": []}
        mock_teardown.return_value = None
        mock_capture.return_value = {"warehouse-ops": []}
        mock_exec.side_effect = RuntimeError("executor crashed unexpectedly")

        verdict = run_pipeline(
            proposed_fix=_correct_fix(),
            target_namespaces=["warehouse-ops"],
            reproducibility="full",
        )

        assert verdict.verdict == Verdict.FAILED
        mock_teardown.assert_called_once()
