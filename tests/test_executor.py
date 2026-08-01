from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from simulate_mcp.executor import execute_fix
from simulate_mcp.models import ProposedFix


def _make_fix(commands=None, manifests=None):
    return ProposedFix(
        commands=commands or [],
        manifests=manifests or [],
        fault_description="test fault",
    )


class TestCommandExecution:
    @patch("simulate_mcp.executor.emit_phase_event")
    @patch("simulate_mcp.executor.subprocess.run")
    def test_runs_kubectl_with_kubeconfig(self, mock_run, _emit):
        mock_run.return_value = MagicMock(
            stdout=b"deployment restarted",
            stderr=b"",
            returncode=0,
        )
        fix = _make_fix(commands=["kubectl rollout restart deployment/app"])

        result = execute_fix("/tmp/kc", fix)

        cmd = mock_run.call_args_list[0][0][0]
        assert cmd[0] == "kubectl"
        assert "--kubeconfig" in cmd
        assert "/tmp/kc" in cmd
        assert "rollout" in cmd
        assert "restart" in cmd

    @patch("simulate_mcp.executor.emit_phase_event")
    @patch("simulate_mcp.executor.subprocess.run")
    def test_strips_leading_kubectl_from_command(self, mock_run, _emit):
        mock_run.return_value = MagicMock(stdout=b"", stderr=b"", returncode=0)
        fix = _make_fix(commands=["kubectl get pods"])

        execute_fix("/tmp/kc", fix)

        cmd = mock_run.call_args_list[0][0][0]
        assert cmd.count("kubectl") == 1

    @patch("simulate_mcp.executor.emit_phase_event")
    @patch("simulate_mcp.executor.subprocess.run")
    def test_captures_stdout_stderr_exit_code(self, mock_run, _emit):
        mock_run.return_value = MagicMock(
            stdout=b"output data",
            stderr=b"warning message",
            returncode=0,
        )
        fix = _make_fix(commands=["kubectl get pods"])

        result = execute_fix("/tmp/kc", fix)

        assert result["commands"][0]["stdout"] == "output data"
        assert result["commands"][0]["stderr"] == "warning message"
        assert result["commands"][0]["exit_code"] == 0

    @patch("simulate_mcp.executor.emit_phase_event")
    @patch("simulate_mcp.executor.subprocess.run")
    def test_handles_nonzero_exit(self, mock_run, _emit):
        mock_run.return_value = MagicMock(
            stdout=b"",
            stderr=b"error: not found",
            returncode=1,
        )
        fix = _make_fix(commands=["kubectl delete pod/missing"])

        result = execute_fix("/tmp/kc", fix)

        assert result["commands"][0]["exit_code"] == 1
        assert "not found" in result["commands"][0]["stderr"]

    @patch("simulate_mcp.executor.emit_phase_event")
    @patch("simulate_mcp.executor.subprocess.run")
    def test_handles_command_timeout(self, mock_run, _emit):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="kubectl", timeout=60)
        fix = _make_fix(commands=["kubectl apply -f big.yaml"])

        result = execute_fix("/tmp/kc", fix)

        assert result["commands"][0]["exit_code"] == -1
        assert "timed out" in result["commands"][0]["stderr"]

    @patch("simulate_mcp.executor.emit_phase_event")
    @patch("simulate_mcp.executor.subprocess.run")
    def test_multiple_commands_all_captured(self, mock_run, _emit):
        mock_run.return_value = MagicMock(stdout=b"ok", stderr=b"", returncode=0)
        fix = _make_fix(commands=["kubectl get pods", "kubectl get svc", "kubectl get ns"])

        result = execute_fix("/tmp/kc", fix)

        assert len(result["commands"]) == 3
        assert all(c["exit_code"] == 0 for c in result["commands"])


class TestManifestApplication:
    @patch("simulate_mcp.executor.emit_phase_event")
    @patch("simulate_mcp.executor.subprocess.run")
    def test_applies_manifest_via_stdin(self, mock_run, _emit):
        mock_run.return_value = MagicMock(returncode=0)
        manifest = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "test"}}
        fix = _make_fix(manifests=[manifest])

        result = execute_fix("/tmp/kc", fix)

        cmd = mock_run.call_args_list[0][0][0]
        assert "kubectl" in cmd
        assert "apply" in cmd
        assert "--kubeconfig" in cmd
        assert "-f" in cmd
        assert "-" in cmd
        assert result["manifests_applied"] == 1
        assert result["manifests_failed"] == 0

    @patch("simulate_mcp.executor.emit_phase_event")
    @patch("simulate_mcp.executor.subprocess.run")
    def test_counts_failed_manifests(self, mock_run, _emit):
        mock_run.side_effect = subprocess.CalledProcessError(1, "kubectl", stderr=b"invalid")
        manifest = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "bad"}}
        fix = _make_fix(manifests=[manifest])

        result = execute_fix("/tmp/kc", fix)

        assert result["manifests_applied"] == 0
        assert result["manifests_failed"] == 1

    @patch("simulate_mcp.executor.emit_phase_event")
    @patch("simulate_mcp.executor.subprocess.run")
    def test_mixed_commands_and_manifests(self, mock_run, _emit):
        mock_run.return_value = MagicMock(stdout=b"ok", stderr=b"", returncode=0)
        fix = _make_fix(
            commands=["kubectl get pods"],
            manifests=[{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cm1"}}],
        )

        result = execute_fix("/tmp/kc", fix)

        assert len(result["commands"]) == 1
        assert result["manifests_applied"] == 1


class TestTelemetry:
    @patch("simulate_mcp.executor.subprocess.run")
    @patch("simulate_mcp.executor.emit_phase_event")
    def test_emits_started_and_completed(self, mock_emit, mock_run):
        mock_run.return_value = MagicMock(stdout=b"", stderr=b"", returncode=0)
        fix = _make_fix(commands=["kubectl get pods"])

        execute_fix("/tmp/kc", fix)

        statuses = [c[0][1] for c in mock_emit.call_args_list]
        assert "started" in statuses
        assert "completed" in statuses


class TestEmptyFix:
    @patch("simulate_mcp.executor.emit_phase_event")
    @patch("simulate_mcp.executor.subprocess.run")
    def test_no_commands_no_manifests(self, mock_run, _emit):
        fix = _make_fix()

        result = execute_fix("/tmp/kc", fix)

        assert result["commands"] == []
        assert result["manifests_applied"] == 0
        assert result["manifests_failed"] == 0
        mock_run.assert_not_called()
