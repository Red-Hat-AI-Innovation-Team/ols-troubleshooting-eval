from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from ols_eval.cluster import (
    _cluster_name,
    _kubeconfig_path,
    _which_provider,
    apply_snapshot,
    provision,
    reset,
    teardown,
)


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    _which_provider.cache_clear()
    yield
    _which_provider.cache_clear()


def _make_manifest(kind: str, name: str, namespace: str = "app-ns") -> dict:
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace},
    }


class TestWhichProvider:
    def test_prefers_k3d(self):
        with patch("ols_eval.cluster.shutil.which") as mock_which:
            mock_which.side_effect = lambda cmd: "/usr/local/bin/k3d" if cmd == "k3d" else None
            assert _which_provider() == "k3d"

    def test_falls_back_to_minikube(self):
        with patch("ols_eval.cluster.shutil.which") as mock_which:
            mock_which.side_effect = lambda cmd: "/usr/local/bin/minikube" if cmd == "minikube" else None
            assert _which_provider() == "minikube"

    def test_raises_when_neither_available(self):
        with patch("ols_eval.cluster.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="Neither k3d nor minikube"):
                _which_provider()

    def test_result_is_cached(self):
        with patch("ols_eval.cluster.shutil.which") as mock_which:
            mock_which.side_effect = lambda cmd: "/usr/local/bin/k3d" if cmd == "k3d" else None
            assert _which_provider() == "k3d"
            assert _which_provider() == "k3d"
            assert mock_which.call_count == 1


class TestProvisionK3d:
    @patch("ols_eval.cluster._which_provider", return_value="k3d")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_calls_k3d_create_with_correct_args(self, _emit, mock_run, _prov):
        mock_run.return_value = MagicMock(stdout=b"kubeconfig-data")

        result = provision("test-123")

        create_call = mock_run.call_args_list[0]
        cmd = create_call[0][0]
        assert cmd[0] == "k3d"
        assert cmd[1] == "cluster"
        assert cmd[2] == "create"
        assert cmd[3] == "factory-sim-test-123"
        assert "--agents" in cmd
        assert cmd[cmd.index("--agents") + 1] == "0"
        assert "--no-lb" in cmd
        assert "--disable=traefik@server:0" in cmd[-1]
        assert create_call[1]["timeout"] == 60
        assert create_call[1]["check"] is True

    @patch("ols_eval.cluster._which_provider", return_value="k3d")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_extracts_kubeconfig(self, _emit, mock_run, _prov, tmp_path, monkeypatch):
        mock_run.return_value = MagicMock(stdout=b"kubeconfig-yaml-data")

        kc_path = str(tmp_path / "factory-sim-test-123.kubeconfig")
        monkeypatch.setattr("ols_eval.cluster._kubeconfig_path", lambda tid: kc_path)

        result = provision("test-123")

        assert result == kc_path
        kc_call = mock_run.call_args_list[1]
        assert kc_call[0][0] == ["k3d", "kubeconfig", "get", "factory-sim-test-123"]

    @patch("ols_eval.cluster._which_provider", return_value="k3d")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_returns_kubeconfig_path(self, _emit, mock_run, _prov):
        mock_run.return_value = MagicMock(stdout=b"data")
        result = provision("abc")
        assert result == _kubeconfig_path("abc")

    @patch("ols_eval.cluster._which_provider", return_value="k3d")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_emits_telemetry(self, mock_emit, mock_run, _prov):
        mock_run.return_value = MagicMock(stdout=b"data")
        provision("t1")
        phases = [c[0][1] for c in mock_emit.call_args_list]
        assert "started" in phases
        assert "completed" in phases


class TestProvisionMinikube:
    @patch("ols_eval.cluster._which_provider", return_value="minikube")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_calls_minikube_start_with_correct_args(self, _emit, mock_run, _prov, tmp_path, monkeypatch):
        mock_run.return_value = MagicMock(stdout=b"kubeconfig-data")
        kc_path = str(tmp_path / "factory-sim-mk.kubeconfig")
        monkeypatch.setattr("ols_eval.cluster._kubeconfig_path", lambda tid: kc_path)

        provision("mk")

        start_call = mock_run.call_args_list[0]
        cmd = start_call[0][0]
        assert cmd[0] == "minikube"
        assert cmd[1] == "start"
        assert "--profile" in cmd
        assert cmd[cmd.index("--profile") + 1] == "factory-sim-mk"
        assert "--driver" in cmd
        assert cmd[cmd.index("--driver") + 1] == "docker"
        assert "--memory" in cmd
        assert cmd[cmd.index("--memory") + 1] == "1024"
        assert "--cpus" in cmd
        assert cmd[cmd.index("--cpus") + 1] == "1"
        assert start_call[1]["timeout"] == 90


class TestProvisionErrors:
    @patch("ols_eval.cluster._which_provider", return_value="k3d")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_timeout_raises_runtime_error(self, _emit, mock_run, _prov):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="k3d", timeout=60)
        with pytest.raises(RuntimeError, match="timed out"):
            provision("t1")

    @patch("ols_eval.cluster._which_provider", return_value="k3d")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_called_process_error_raises_runtime_error(self, _emit, mock_run, _prov):
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "k3d", stderr=b"cluster already exists"
        )
        with pytest.raises(RuntimeError, match="provision failed"):
            provision("t1")


class TestApplySnapshot:
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_groups_manifests_by_namespace(self, _emit, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        manifests = [
            _make_manifest("Deployment", "app1", "ns-a"),
            _make_manifest("Service", "svc1", "ns-a"),
            _make_manifest("Deployment", "app2", "ns-b"),
        ]

        result = apply_snapshot("/tmp/kc", manifests)

        ns_create_calls = [
            c for c in mock_run.call_args_list
            if "create" in c[0][0] and "namespace" in c[0][0]
        ]
        created_namespaces = [c[0][0][3] for c in ns_create_calls]
        assert "ns-a" in created_namespaces
        assert "ns-b" in created_namespaces

    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_applies_manifests_with_kubectl(self, _emit, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        manifests = [_make_manifest("Deployment", "app1")]

        result = apply_snapshot("/tmp/kc", manifests)

        apply_calls = [
            c for c in mock_run.call_args_list
            if "apply" in c[0][0]
        ]
        assert len(apply_calls) == 1
        cmd = apply_calls[0][0][0]
        assert "kubectl" in cmd
        assert "--kubeconfig" in cmd
        assert "/tmp/kc" in cmd

    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_returns_correct_counts(self, _emit, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        manifests = [
            _make_manifest("Deployment", "app1"),
            _make_manifest("Service", "svc1"),
        ]

        result = apply_snapshot("/tmp/kc", manifests)

        assert result["applied"] == 2
        assert result["failed"] == 0
        assert result["errors"] == []

    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_counts_failures(self, _emit, mock_run):
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "apply" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr=b"invalid manifest")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        manifests = [_make_manifest("Deployment", "app1")]

        result = apply_snapshot("/tmp/kc", manifests)

        assert result["applied"] == 0
        assert result["failed"] == 1
        assert len(result["errors"]) == 1

    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_skips_system_namespace_creation(self, _emit, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        manifests = [_make_manifest("ConfigMap", "cm1", "kube-system")]

        apply_snapshot("/tmp/kc", manifests)

        ns_create_calls = [
            c for c in mock_run.call_args_list
            if "create" in c[0][0] and "namespace" in c[0][0]
        ]
        assert len(ns_create_calls) == 0

    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_empty_manifests(self, _emit, mock_run):
        result = apply_snapshot("/tmp/kc", [])
        assert result == {"applied": 0, "failed": 0, "errors": []}

    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_handles_timeout_on_apply(self, _emit, mock_run):
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "apply" in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        manifests = [_make_manifest("Deployment", "app1")]

        result = apply_snapshot("/tmp/kc", manifests)

        assert result["failed"] == 1
        assert "timeout" in result["errors"][0]


class TestTeardown:
    @patch("ols_eval.cluster._which_provider", return_value="k3d")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_calls_k3d_delete(self, _emit, mock_run, _prov):
        teardown("t1")
        cmd = mock_run.call_args_list[0][0][0]
        assert cmd == ["k3d", "cluster", "delete", "factory-sim-t1"]

    @patch("ols_eval.cluster._which_provider", return_value="minikube")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_calls_minikube_delete(self, _emit, mock_run, _prov):
        teardown("t1")
        cmd = mock_run.call_args_list[0][0][0]
        assert cmd == ["minikube", "delete", "--profile", "factory-sim-t1"]

    @patch("ols_eval.cluster._which_provider", return_value="k3d")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_safe_when_cluster_does_not_exist(self, _emit, mock_run, _prov):
        mock_run.side_effect = subprocess.CalledProcessError(1, "k3d", stderr=b"not found")
        teardown("nonexistent")

    @patch("ols_eval.cluster._which_provider", return_value="k3d")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_safe_on_timeout(self, _emit, mock_run, _prov):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="k3d", timeout=60)
        teardown("slow")

    @patch("ols_eval.cluster._which_provider", return_value="k3d")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_cleans_up_kubeconfig_file(self, _emit, mock_run, _prov, tmp_path, monkeypatch):
        kc_path = str(tmp_path / "factory-sim-t1.kubeconfig")
        Path(kc_path).write_text("kubeconfig-data")
        monkeypatch.setattr("ols_eval.cluster._kubeconfig_path", lambda tid: kc_path)

        teardown("t1")

        assert not Path(kc_path).exists()

    @patch("ols_eval.cluster._which_provider", return_value="k3d")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_emits_telemetry(self, mock_emit, mock_run, _prov):
        teardown("t1")
        phases = [c[0][1] for c in mock_emit.call_args_list]
        assert "started" in phases
        assert "completed" in phases


class TestReset:
    @patch("ols_eval.cluster.apply_snapshot")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_deletes_non_system_namespaces(self, _emit, mock_run, mock_apply):
        mock_run.return_value = MagicMock(
            stdout=b"default kube-system kube-public app-ns custom-ns"
        )
        mock_apply.return_value = {"applied": 1, "failed": 0, "errors": []}
        manifests = [_make_manifest("Deployment", "app1")]

        reset("t1", manifests)

        delete_calls = [
            c for c in mock_run.call_args_list
            if "delete" in c[0][0] and "namespace" in c[0][0]
        ]
        deleted = {c[0][0][3] for c in delete_calls}
        assert "app-ns" in deleted
        assert "custom-ns" in deleted
        assert "kube-system" not in deleted
        assert "default" not in deleted

    @patch("ols_eval.cluster.apply_snapshot")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_re_applies_manifests(self, _emit, mock_run, mock_apply):
        mock_run.return_value = MagicMock(stdout=b"default")
        mock_apply.return_value = {"applied": 2, "failed": 0, "errors": []}
        manifests = [_make_manifest("Deployment", "app1"), _make_manifest("Service", "svc1")]

        result = reset("t1", manifests)

        mock_apply.assert_called_once()
        assert result["applied"] == 2

    @patch("ols_eval.cluster.apply_snapshot")
    @patch("ols_eval.cluster.subprocess.run")
    @patch("ols_eval.cluster.emit_phase_event")
    def test_handles_namespace_list_failure(self, _emit, mock_run, mock_apply):
        mock_run.side_effect = subprocess.CalledProcessError(1, "kubectl")
        mock_apply.return_value = {"applied": 1, "failed": 0, "errors": []}
        manifests = [_make_manifest("Deployment", "app1")]

        result = reset("t1", manifests)

        mock_apply.assert_called_once()


@pytest.mark.integration
class TestClusterIntegration:
    """Integration tests that require k3d/minikube and Docker.

    Run with: pytest -m integration
    Skip in CI without Docker.
    """

    def test_provision_and_teardown(self):
        pytest.skip("Requires Docker and k3d/minikube — run manually with: pytest -m integration --no-header")
