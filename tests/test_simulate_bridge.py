"""Unit tests for simulate_mcp.simulate_bridge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simulate_mcp.simulate_bridge import (
    DEFAULT_MICROSHIFT_PORT,
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


class TestRunSimulation:
    def test_returns_failed_when_no_reports(self, tmp_project: Path) -> None:
        verdict = run_simulation(project_path=str(tmp_project))
        assert verdict["verdict"] == "FAILED"

    def test_returns_fixed_when_verify_report_has_perfect_score(
        self, tmp_project: Path
    ) -> None:
        verify = tmp_project / ".factory" / "simulate" / "verify-report.md"
        verify.write_text("## Verification\n\nStructural health score: 1.0\n")
        verdict = run_simulation(project_path=str(tmp_project))
        assert verdict["verdict"] == "FIXED_HIGH_CONFIDENCE"

    def test_returns_partial_when_verify_report_has_partial_score(
        self, tmp_project: Path
    ) -> None:
        verify = tmp_project / ".factory" / "simulate" / "verify-report.md"
        verify.write_text("## Verification\n\nStructural health score: 0.5\n")
        verdict = run_simulation(project_path=str(tmp_project))
        assert verdict["verdict"] == "PARTIAL"

    def test_writes_verdict_json(self, tmp_project: Path) -> None:
        run_simulation(project_path=str(tmp_project))
        verdict_path = tmp_project / ".factory" / "simulate" / "verdict.json"
        assert verdict_path.exists()
        data = json.loads(verdict_path.read_text())
        assert "verdict" in data
        assert "evidence" in data

    def test_includes_fix_report_in_evidence(self, tmp_project: Path) -> None:
        fix = tmp_project / ".factory" / "simulate" / "fix-report.md"
        fix.write_text("## Fix Report\n\ncommand: set env ...\nexit code: 0\n")
        verdict = run_simulation(project_path=str(tmp_project))
        assert "fix_report" in verdict["evidence"]


class TestFormatFailureContext:
    def test_fixed_verdict_returns_success_message(self) -> None:
        verdict = {"verdict": "FIXED_HIGH_CONFIDENCE", "evidence": {}}
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

    def test_partial_verdict_includes_topology_score(self) -> None:
        verdict = {
            "verdict": "PARTIAL",
            "evidence": {"topology_match": 0.5, "logs": []},
        }
        result = format_failure_context(verdict)
        assert "0.5" in result

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


class TestCLI:
    def test_no_command_returns_1(self) -> None:
        assert main([]) == 1

    def test_validate_missing_args_raises(self) -> None:
        with pytest.raises(SystemExit):
            main(["validate"])

    def test_validate_runs_end_to_end(
        self, fix_proposal_file: Path, tmp_project: Path
    ) -> None:
        result = main([
            "validate",
            "--fix-proposal", str(fix_proposal_file),
            "--kubeconfig", "/tmp/kc",
            "--project", str(tmp_project),
        ])
        assert result == 1
        verdict_path = tmp_project / ".factory" / "simulate" / "verdict.json"
        assert verdict_path.exists()
