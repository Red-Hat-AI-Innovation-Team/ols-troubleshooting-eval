from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from simulate_mcp.healthcheck import (
    _check_readiness,
    _check_restart_stability,
    _check_status_transition,
    _compare_states,
    _parse_pod,
    capture_pod_states,
    verify,
)


def _kubectl_pods_json(pods: list[dict]) -> bytes:
    items = []
    for p in pods:
        items.append({
            "metadata": {"name": p["name"]},
            "status": {
                "phase": p.get("status", "Running"),
                "containerStatuses": [
                    {
                        "ready": p.get("ready", True),
                        "restartCount": p.get("restart_count", 0),
                    }
                ],
            },
        })
    return json.dumps({"items": items}).encode()


class TestParsePod:
    def test_running_pod(self):
        item = {
            "metadata": {"name": "app-abc"},
            "status": {
                "phase": "Running",
                "containerStatuses": [{"ready": True, "restartCount": 0}],
            },
        }
        result = _parse_pod(item)
        assert result == {"name": "app-abc", "status": "Running", "ready": True, "restart_count": 0}

    def test_crashing_pod(self):
        item = {
            "metadata": {"name": "broken-pod"},
            "status": {
                "phase": "CrashLoopBackOff",
                "containerStatuses": [{"ready": False, "restartCount": 15}],
            },
        }
        result = _parse_pod(item)
        assert result["status"] == "CrashLoopBackOff"
        assert result["ready"] is False
        assert result["restart_count"] == 15

    def test_no_container_statuses(self):
        item = {"metadata": {"name": "pending"}, "status": {"phase": "Pending"}}
        result = _parse_pod(item)
        assert result["ready"] is False
        assert result["restart_count"] == 0


class TestCapturePodStates:
    @patch("simulate_mcp.healthcheck.subprocess.run")
    def test_captures_pods_per_namespace(self, mock_run):
        pods = [{"name": "app-1", "status": "Running"}]
        mock_run.return_value = MagicMock(stdout=_kubectl_pods_json(pods))

        result = capture_pod_states("/tmp/kc", ["app-ns"])

        assert "app-ns" in result
        assert len(result["app-ns"]) == 1
        assert result["app-ns"][0]["name"] == "app-1"

    @patch("simulate_mcp.healthcheck.subprocess.run")
    def test_multiple_namespaces(self, mock_run):
        pods = [{"name": "pod-1"}]
        mock_run.return_value = MagicMock(stdout=_kubectl_pods_json(pods))

        result = capture_pod_states("/tmp/kc", ["ns-a", "ns-b"])

        assert "ns-a" in result
        assert "ns-b" in result

    @patch("simulate_mcp.healthcheck.subprocess.run")
    def test_returns_empty_on_error(self, mock_run):
        import subprocess as sp
        mock_run.side_effect = sp.CalledProcessError(1, "kubectl")

        result = capture_pod_states("/tmp/kc", ["bad-ns"])

        assert result["bad-ns"] == []


class TestStatusTransition:
    def test_crash_to_running(self):
        before = {"name": "p", "status": "CrashLoopBackOff", "ready": False, "restart_count": 5}
        after = {"name": "p", "status": "Running", "ready": True, "restart_count": 5}
        check = _check_status_transition("ns", "p", before, after)
        assert check["passed"] is True
        assert "CrashLoopBackOff -> Running" in check["detail"]

    def test_still_crashing(self):
        before = {"name": "p", "status": "CrashLoopBackOff", "ready": False, "restart_count": 5}
        after = {"name": "p", "status": "Error", "ready": False, "restart_count": 6}
        check = _check_status_transition("ns", "p", before, after)
        assert check["passed"] is False
        assert "still in bad state" in check["detail"]

    def test_regression(self):
        before = {"name": "p", "status": "Running", "ready": True, "restart_count": 0}
        after = {"name": "p", "status": "CrashLoopBackOff", "ready": False, "restart_count": 3}
        check = _check_status_transition("ns", "p", before, after)
        assert check["passed"] is False
        assert "regressed" in check["detail"]

    def test_running_stays_running(self):
        before = {"name": "p", "status": "Running", "ready": True, "restart_count": 0}
        after = {"name": "p", "status": "Running", "ready": True, "restart_count": 0}
        check = _check_status_transition("ns", "p", before, after)
        assert check["passed"] is True


class TestReadiness:
    def test_ready_pod(self):
        after = {"name": "p", "status": "Running", "ready": True, "restart_count": 0}
        check = _check_readiness("ns", "p", after)
        assert check["passed"] is True

    def test_not_ready_pod(self):
        after = {"name": "p", "status": "Running", "ready": False, "restart_count": 0}
        check = _check_readiness("ns", "p", after)
        assert check["passed"] is False


class TestRestartStability:
    def test_stable_restarts(self):
        before = {"name": "p", "status": "Running", "ready": True, "restart_count": 3}
        after = {"name": "p", "status": "Running", "ready": True, "restart_count": 3}
        check = _check_restart_stability("ns", "p", before, after)
        assert check["passed"] is True
        assert "delta=0" in check["detail"]

    def test_increasing_restarts_fails(self):
        before = {"name": "p", "status": "Running", "ready": True, "restart_count": 3}
        after = {"name": "p", "status": "Running", "ready": True, "restart_count": 7}
        check = _check_restart_stability("ns", "p", before, after)
        assert check["passed"] is False
        assert "delta=4" in check["detail"]


class TestCompareStates:
    def test_single_pod_improvement(self):
        before = {"ns": [{"name": "app", "status": "CrashLoopBackOff", "ready": False, "restart_count": 5}]}
        after = {"ns": [{"name": "app", "status": "Running", "ready": True, "restart_count": 5}]}
        checks = _compare_states(before, after)
        assert len(checks) == 3
        assert all(c["passed"] for c in checks)

    def test_empty_states(self):
        checks = _compare_states({}, {})
        assert checks == []


class TestVerify:
    @patch("simulate_mcp.healthcheck.emit_phase_event")
    @patch("simulate_mcp.healthcheck.capture_pod_states")
    def test_passes_when_all_checks_pass(self, mock_capture, _emit):
        before = {"ns": [{"name": "app", "status": "CrashLoopBackOff", "ready": False, "restart_count": 5}]}
        after = {"ns": [{"name": "app", "status": "Running", "ready": True, "restart_count": 5}]}
        mock_capture.return_value = after

        result = verify("/tmp/kc", ["ns"], before)

        assert result["passed"] is True
        assert result["before_state"] == before
        assert result["after_state"] == after

    @patch("simulate_mcp.healthcheck.emit_phase_event")
    @patch("simulate_mcp.healthcheck.capture_pod_states")
    def test_fails_when_pod_still_crashing(self, mock_capture, _emit):
        before = {"ns": [{"name": "app", "status": "CrashLoopBackOff", "ready": False, "restart_count": 5}]}
        after = {"ns": [{"name": "app", "status": "CrashLoopBackOff", "ready": False, "restart_count": 8}]}
        mock_capture.return_value = after

        result = verify("/tmp/kc", ["ns"], before)

        assert result["passed"] is False

    @patch("simulate_mcp.healthcheck.emit_phase_event")
    @patch("simulate_mcp.healthcheck.capture_pod_states")
    def test_emits_telemetry(self, mock_capture, mock_emit):
        mock_capture.return_value = {}

        verify("/tmp/kc", ["ns"], {})

        statuses = [c[0][1] for c in mock_emit.call_args_list]
        assert "started" in statuses
        assert "completed" in statuses
