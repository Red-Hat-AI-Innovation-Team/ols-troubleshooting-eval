"""Integration tests for the simulate-validate retry loop."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ols_eval.simulate_bridge import (
    format_failure_context,
    main,
    prepare_simulation,
    run_simulation,
)


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    simulate_dir = tmp_path / ".factory" / "simulate"
    simulate_dir.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def fix_proposal_file(tmp_path: Path) -> Path:
    proposal = {
        "commands": [
            "set env deployment/nginx-app APP_CONFIG_PATH=/etc/app/config -n simulate-test"
        ],
        "manifests": [],
        "fault_description": "APP_CONFIG_PATH environment variable is missing",
        "verification_checks": ["pod nginx-app should be Running"],
    }
    path = tmp_path / "proposed-fix.json"
    path.write_text(json.dumps(proposal))
    return path


def _make_invoke_side_effect(project_path: Path, verdicts_by_call: list[tuple[str, float]]):
    """Build a side_effect for _invoke_factory_agent that writes verify-report.md
    with the appropriate score on each full simulation run.

    verdicts_by_call is a list of (verdict_name, score) tuples — one per
    run_simulation() invocation.  Each run invokes the factory agent 5 times
    (once per WORKFLOW_PHASES entry), so the side_effect writes the
    verify-report on the 5th call of each group.
    """
    call_counter = {"n": 0, "run": 0}
    simulate_dir = project_path / ".factory" / "simulate"

    def side_effect(role, task_description, project_path, timeout):
        call_counter["n"] += 1

        # 5 phases per run — write the verify-report on the last call
        if call_counter["n"] % 5 == 0:
            run_idx = call_counter["run"]
            call_counter["run"] += 1

            if run_idx < len(verdicts_by_call):
                _, score = verdicts_by_call[run_idx]
                verify_report = simulate_dir / "verify-report.md"
                verify_report.write_text(
                    f"## Verification\n\nStructural health score: {score}\n"
                )

                if score < 1.0:
                    fix_report = simulate_dir / "fix-report.md"
                    fix_report.write_text(
                        "command: set env ...\nstderr: env var not applied\n"
                        "exit code: 1\nerror: deployment still failing\n"
                    )

        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="done", stderr=""
        )

    return side_effect


class TestRetryLoopFailedThenFixed:
    """First simulation returns FAILED, second returns FIXED."""

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_failed_then_fixed(
        self, mock_invoke: MagicMock, fix_proposal_file: Path, tmp_project: Path
    ) -> None:
        verdicts = [("FAILED", 0.2), ("FIXED_HIGH_CONFIDENCE", 1.0)]
        mock_invoke.side_effect = _make_invoke_side_effect(tmp_project, verdicts)

        # Attempt 1: should fail
        prepare_simulation(
            str(fix_proposal_file), "/tmp/kc", str(tmp_project)
        )
        verdict_1 = run_simulation(str(tmp_project))
        assert verdict_1["verdict"] == "FAILED"

        context = format_failure_context(verdict_1)
        assert "did **not** resolve" in context
        assert "revise your diagnosis" in context.lower()

        # Attempt 2: should succeed
        verdict_2 = run_simulation(str(tmp_project))
        assert verdict_2["verdict"] == "FIXED_HIGH_CONFIDENCE"
        assert verdict_2["scenario_reproducibility"] == "full"


class TestAllAttemptsFail:
    """All 3 retry attempts fail — agent should return LOW confidence."""

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_three_failures_returns_low_confidence(
        self, mock_invoke: MagicMock, fix_proposal_file: Path, tmp_project: Path
    ) -> None:
        verdicts = [("FAILED", 0.1), ("FAILED", 0.2), ("FAILED", 0.3)]
        mock_invoke.side_effect = _make_invoke_side_effect(tmp_project, verdicts)

        prepare_simulation(
            str(fix_proposal_file), "/tmp/kc", str(tmp_project)
        )

        best_verdict = None
        best_score = -1.0

        for attempt in range(3):
            verdict = run_simulation(str(tmp_project))
            assert verdict["verdict"] in ("FAILED", "PARTIAL")
            score = verdict["evidence"].get("topology_match", 0.0)

            if score > best_score:
                best_score = score
                best_verdict = verdict

            if verdict["verdict"].startswith("FIXED"):
                break

            context = format_failure_context(verdict)
            assert "revise your diagnosis" in context.lower()

        assert best_verdict is not None
        assert not best_verdict["verdict"].startswith("FIXED")
        assert best_score < 1.0


class TestFirstAttemptSucceeds:
    """First attempt returns FIXED — no retries needed."""

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_immediate_success(
        self, mock_invoke: MagicMock, fix_proposal_file: Path, tmp_project: Path
    ) -> None:
        verdicts = [("FIXED_HIGH_CONFIDENCE", 1.0)]
        mock_invoke.side_effect = _make_invoke_side_effect(tmp_project, verdicts)

        prepare_simulation(
            str(fix_proposal_file), "/tmp/kc", str(tmp_project)
        )

        verdict = run_simulation(str(tmp_project))
        assert verdict["verdict"] == "FIXED_HIGH_CONFIDENCE"
        assert verdict["scenario_reproducibility"] == "full"

        context = format_failure_context(verdict)
        assert "validated successfully" in context


class TestPartialThenFixed:
    """First attempt returns PARTIAL, second returns FIXED."""

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_partial_then_fixed(
        self, mock_invoke: MagicMock, fix_proposal_file: Path, tmp_project: Path
    ) -> None:
        verdicts = [("PARTIAL", 0.7), ("FIXED_HIGH_CONFIDENCE", 1.0)]
        mock_invoke.side_effect = _make_invoke_side_effect(tmp_project, verdicts)

        prepare_simulation(
            str(fix_proposal_file), "/tmp/kc", str(tmp_project)
        )

        verdict_1 = run_simulation(str(tmp_project))
        assert verdict_1["verdict"] == "PARTIAL"
        assert verdict_1["scenario_reproducibility"] == "structural"

        context = format_failure_context(verdict_1)
        assert "partially" in context.lower()

        verdict_2 = run_simulation(str(tmp_project))
        assert verdict_2["verdict"] == "FIXED_HIGH_CONFIDENCE"


class TestCLIWithSkillFlag:
    """CLI validate subcommand accepts --skill and includes it in verdict."""

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_default_skill(
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
        verdict_path = tmp_project / ".factory" / "simulate" / "verdict.json"
        verdict = json.loads(verdict_path.read_text())
        assert verdict["skill"] == "simulate-validate"
        assert result == 1

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_custom_skill(
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
            "--skill", "custom-validation-skill",
        ])
        verdict_path = tmp_project / ".factory" / "simulate" / "verdict.json"
        verdict = json.loads(verdict_path.read_text())
        assert verdict["skill"] == "custom-validation-skill"
        assert result == 1

    @patch("ols_eval.simulate_bridge._invoke_factory_agent")
    def test_skill_persists_on_fixed_verdict(
        self,
        mock_invoke: MagicMock,
        fix_proposal_file: Path,
        tmp_project: Path,
    ) -> None:
        mock_invoke.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        verify = tmp_project / ".factory" / "simulate" / "verify-report.md"
        verify.write_text("Structural health score: 1.0\n")
        result = main([
            "validate",
            "--fix-proposal", str(fix_proposal_file),
            "--kubeconfig", "/tmp/kc",
            "--project", str(tmp_project),
            "--skill", "simulate-validate",
        ])
        verdict_path = tmp_project / ".factory" / "simulate" / "verdict.json"
        verdict = json.loads(verdict_path.read_text())
        assert verdict["skill"] == "simulate-validate"
        assert verdict["verdict"] == "FIXED_HIGH_CONFIDENCE"
        assert result == 0
