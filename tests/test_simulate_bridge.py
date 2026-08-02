"""Unit tests for ols_eval.simulate_bridge."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from ols_eval.simulate_bridge import (
    DEFAULT_MICROSHIFT_PORT,
    WORKFLOW_PHASES,
    _extract_failed_checks,
    _extract_log_entries,
    _invoke_factory_agent,
    _parse_verify_report,
    format_failure_context,
    main,
    prepare_simulation,
    run_simulation,
)


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a minimal project directory with .factory/simulate/."""
    simulate_dir = tmp_path / ".factory" / "simulate"
    simulate_dir.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def fix_proposal_file(tmp_path: Path) -> Path:
    """Write a sample proposed fix JSON and return its path."""
    proposal = {
        "commands": [
            "set env deployment/nginx-app APP_CONFIG_PATH=/etc/app/config -n simulate-test"
        ],
        "manifests": [],
        "fault_description": "APP_CONFIG_PATH environment variable is missing",
    }
    path = tmp_path / "proposed-fix.json"
    path.write_text(json.dumps(proposal))
    return path


class TestPrepareSimulation:
    def test_writes_task_json(
        self, fix_proposal_file: Path, tmp_project: Path
    ) -> None:
        kubeconfig = "/tmp/kubeconfig"
        result = prepare_simulation(
            fix_proposal_path=str(fix_proposal_file),
            target_kubeconfig=kubeconfig,
            project_path=str(tmp_project),
        )
        task_path = Path(result)
        assert task_path.exists()
        task = json.loads(task_path.read_text())
        assert task["cluster_type"] == "microshift"
        assert task["microshift_port"] == DEFAULT_MICROSHIFT_PORT
        assert task["fix_commands"] == [
            "set env deployment/nginx-app APP_CONFIG_PATH=/etc/app/config -n simulate-test"
        ]

    def test_extracts_namespaces_from_commands(
        self, fix_proposal_file: Path, tmp_project: Path
    ) -> None:
        result = prepare_simulation(
            fix_proposal_path=str(fix_proposal_file),
            target_kubeconfig="/tmp/kc",
            project_path=str(tmp_project),
        )
        task = json.loads(Path(result).read_text())
        assert "simulate-test" in task["namespaces"]

    def test_defaults_namespace_when_none_extracted(
        self, tmp_path: Path, tmp_project: Path
    ) -> None:
        proposal = {
            "commands": ["get pods"],
            "manifests": [],
            "fault_description": "unknown issue",
        }
        path = tmp_path / "no-ns-fix.json"
        path.write_text(json.dumps(proposal))
        result = prepare_simulation(
            fix_proposal_path=str(path),
            target_kubeconfig="/tmp/kc",
            project_path=str(tmp_project),
        )
        task = json.loads(Path(result).read_text())
        assert task["namespaces"] == ["default"]

    def test_includes_target_kubeconfig(
        self, fix_proposal_file: Path, tmp_project: Path
    ) -> None:
        kc = "/custom/kubeconfig"
        result = prepare_simulation(
            fix_proposal_path=str(fix_proposal_file),
            target_kubeconfig=kc,
            project_path=str(tmp_project),
        )
        task = json.loads(Path(result).read_text())
        assert task["target_kubeconfig"] == kc


class TestParseVerifyReport:
    def test_perfect_score(self) -> None:
        verdict, score = _parse_verify_report("Structural health score: 1.0")
        assert verdict == "FIXED_HIGH_CONFIDENCE"
        assert score == 1.0

    def test_partial_score(self) -> None:
        verdict, score = _parse_verify_report("score: 0.7\nSome checks passed")
        assert verdict == "PARTIAL"
        assert score == 0.7

    def test_low_score(self) -> None:
        verdict, score = _parse_verify_report("score: 0.2\nMost checks failed")
        assert verdict == "FAILED"
        assert score == 0.2

    def test_no_score_found(self) -> None:
        verdict, score = _parse_verify_report("No numeric data here")
        assert verdict == "FAILED"
        assert score == 0.0


class TestExtractFailedChecks:
    def test_extracts_failure_lines(self) -> None:
        report = "Namespace OK\nDeployment nginx-app: FAIL - not found\nService OK"
        checks = _extract_failed_checks(report)
        assert len(checks) == 1
        assert "FAIL" in checks[0]

    def test_extracts_error_lines(self) -> None:
        report = "error: pod crashed\nmissing configmap\nOK"
        checks = _extract_failed_checks(report)
        assert len(checks) == 2

    def test_limits_to_20(self) -> None:
        report = "\n".join(f"error line {i}" for i in range(30))
        checks = _extract_failed_checks(report)
        assert len(checks) == 20


class TestExtractLogEntries:
    def test_extracts_stderr_and_errors(self) -> None:
        report = "command: set env\nstderr: permission denied\nexit code: 1"
        logs = _extract_log_entries(report)
        assert any("stderr" in l.lower() for l in logs)
        assert any("exit code" in l.lower() for l in logs)

    def test_extracts_crashloopbackoff(self) -> None:
        report = "pod status: CrashLoopBackOff\ncontainer restarted"
        logs = _extract_log_entries(report)
        assert any("CrashLoopBackOff" in l for l in logs)


class TestInvokeFactoryAgent:
    @patch("ols_eval.simulate_bridge.subprocess.run")
    def test_builds_correct_command(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        _invoke_factory_agent("builder", "do stuff", "/proj", 300)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["factory", "agent", "builder"]
        assert "--task" in cmd
        assert "--project" in cmd
        assert "/proj" in cmd
        assert "--timeout" in cmd
        assert "300" in cmd


class TestRunSimulation:
    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_invokes_all_phases_in_order(
        self, mock_invoke: MagicMock, tmp_project: Path
    ) -> None:
        mock_invoke.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="done", stderr=""
        )
        run_simulation(project_path=str(tmp_project))
        assert mock_invoke.call_count == len(WORKFLOW_PHASES)
        roles_called = [call.kwargs["role"] for call in mock_invoke.call_args_list]
        expected_roles = [role for _, role, _ in WORKFLOW_PHASES]
        assert roles_called == expected_roles

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_returns_failed_when_no_reports(
        self, mock_invoke: MagicMock, tmp_project: Path
    ) -> None:
        mock_invoke.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        verdict = run_simulation(project_path=str(tmp_project))
        assert verdict["verdict"] == "FAILED"
        assert "failed_checks" in verdict["evidence"]

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_returns_fixed_when_verify_report_has_perfect_score(
        self, mock_invoke: MagicMock, tmp_project: Path
    ) -> None:
        mock_invoke.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        verify = tmp_project / ".factory" / "simulate" / "verify-report.md"
        verify.write_text("## Verification\n\nStructural health score: 1.0\n")
        verdict = run_simulation(project_path=str(tmp_project))
        assert verdict["verdict"] == "FIXED_HIGH_CONFIDENCE"
        assert verdict["scenario_reproducibility"] == "full"

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_returns_partial_when_verify_report_has_partial_score(
        self, mock_invoke: MagicMock, tmp_project: Path
    ) -> None:
        mock_invoke.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        verify = tmp_project / ".factory" / "simulate" / "verify-report.md"
        verify.write_text("## Verification\n\nStructural health score: 0.5\n")
        verdict = run_simulation(project_path=str(tmp_project))
        assert verdict["verdict"] == "PARTIAL"
        assert verdict["scenario_reproducibility"] == "structural"

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_writes_verdict_json(
        self, mock_invoke: MagicMock, tmp_project: Path
    ) -> None:
        mock_invoke.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        run_simulation(project_path=str(tmp_project))
        verdict_path = tmp_project / ".factory" / "simulate" / "verdict.json"
        assert verdict_path.exists()
        data = json.loads(verdict_path.read_text())
        assert "verdict" in data
        assert "evidence" in data
        assert "execution_time_ms" in data

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_includes_fix_report_in_evidence(
        self, mock_invoke: MagicMock, tmp_project: Path
    ) -> None:
        mock_invoke.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        fix = tmp_project / ".factory" / "simulate" / "fix-report.md"
        fix.write_text("## Fix Report\n\ncommand: set env ...\nexit code: 0\n")
        verdict = run_simulation(project_path=str(tmp_project))
        assert verdict["evidence"]["fix_report"]

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_captures_execution_timing(
        self, mock_invoke: MagicMock, tmp_project: Path
    ) -> None:
        mock_invoke.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        verdict = run_simulation(project_path=str(tmp_project))
        assert verdict["execution_time_ms"] >= 0

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_stops_on_timeout(
        self, mock_invoke: MagicMock, tmp_project: Path
    ) -> None:
        mock_invoke.side_effect = subprocess.TimeoutExpired(cmd="factory", timeout=120)
        verdict = run_simulation(project_path=str(tmp_project))
        assert verdict["verdict"] == "FAILED"
        assert mock_invoke.call_count == 1

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_records_phase_results_in_evidence(
        self, mock_invoke: MagicMock, tmp_project: Path
    ) -> None:
        mock_invoke.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="phase done", stderr=""
        )
        verdict = run_simulation(project_path=str(tmp_project))
        phase_results = verdict["evidence"]["phase_results"]
        assert "snapshot" in phase_results
        assert "verify" in phase_results
        assert phase_results["snapshot"]["returncode"] == 0

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_extracts_log_entries_from_fix_report(
        self, mock_invoke: MagicMock, tmp_project: Path
    ) -> None:
        mock_invoke.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        fix = tmp_project / ".factory" / "simulate" / "fix-report.md"
        fix.write_text("stderr: connection refused\nexit code: 1\n")
        verdict = run_simulation(project_path=str(tmp_project))
        assert len(verdict["evidence"]["logs"]) > 0


class TestFormatFailureContext:
    def test_fixed_verdict_returns_success_message(self) -> None:
        verdict = {"verdict": "FIXED_HIGH_CONFIDENCE", "evidence": {}}
        result = format_failure_context(verdict)
        assert "validated successfully" in result

    def test_fixed_structural_returns_success_message(self) -> None:
        verdict = {"verdict": "FIXED_STRUCTURAL_ONLY", "evidence": {}}
        result = format_failure_context(verdict)
        assert "validated successfully" in result

    def test_failed_verdict_returns_actionable_context(self) -> None:
        verdict = {
            "verdict": "FAILED",
            "evidence": {
                "topology_match": 0.0,
                "logs": ["pod nginx-app CrashLoopBackOff"],
            },
        }
        result = format_failure_context(verdict)
        assert "did **not** resolve" in result
        assert "CrashLoopBackOff" in result
        assert "revise your diagnosis" in result.lower()

    def test_partial_verdict_includes_partial_message(self) -> None:
        verdict = {
            "verdict": "PARTIAL",
            "evidence": {"topology_match": 0.5, "logs": []},
        }
        result = format_failure_context(verdict)
        assert "partially" in result.lower()
        assert "0.5" in result

    def test_includes_failed_checks(self) -> None:
        verdict = {
            "verdict": "FAILED",
            "evidence": {
                "topology_match": 0.0,
                "logs": [],
                "failed_checks": [
                    "Deployment nginx-app: FAIL - not found",
                    "Service nginx-svc: missing endpoints",
                ],
            },
        }
        result = format_failure_context(verdict)
        assert "Verification Checks That Failed" in result
        assert "nginx-app" in result
        assert "missing endpoints" in result

    def test_includes_pod_logs_section(self) -> None:
        verdict = {
            "verdict": "FAILED",
            "evidence": {
                "topology_match": 0.0,
                "logs": ["stderr: connection refused", "exit code: 1"],
            },
        }
        result = format_failure_context(verdict)
        assert "Relevant Pod Logs and Events" in result
        assert "connection refused" in result

    def test_includes_fix_report_excerpt(self) -> None:
        verdict = {
            "verdict": "FAILED",
            "evidence": {
                "topology_match": 0.0,
                "logs": [],
                "fix_report": "command: set env ...\nexit code: 1\nstderr: error",
            },
        }
        result = format_failure_context(verdict)
        assert "Fix Execution Details" in result

    def test_includes_execution_time(self) -> None:
        verdict = {
            "verdict": "FAILED",
            "evidence": {"topology_match": 0.0},
            "execution_time_ms": 5000,
        }
        result = format_failure_context(verdict)
        assert "5000ms" in result


class TestCLI:
    def test_no_command_returns_1(self) -> None:
        assert main([]) == 1

    def test_validate_missing_args_raises(self) -> None:
        with pytest.raises(SystemExit):
            main(["validate"])

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_validate_runs_end_to_end(
        self,
        mock_invoke: MagicMock,
        fix_proposal_file: Path,
        tmp_project: Path,
    ) -> None:
        mock_invoke.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        result = main([
            "validate",
            "--fix-proposal", str(fix_proposal_file),
            "--kubeconfig", "/tmp/kc",
            "--project", str(tmp_project),
        ])
        assert result == 1
        verdict_path = tmp_project / ".factory" / "simulate" / "verdict.json"
        assert verdict_path.exists()
