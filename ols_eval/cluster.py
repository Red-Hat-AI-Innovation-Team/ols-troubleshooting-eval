from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from ols_eval.telemetry import emit_phase_event

_SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "default"}

PROVISION_TIMEOUT = 60
APPLY_TIMEOUT = 30
TEARDOWN_TIMEOUT = 60


@lru_cache(maxsize=1)
def _which_provider() -> str:
    if shutil.which("k3d"):
        return "k3d"
    if shutil.which("minikube"):
        return "minikube"
    raise RuntimeError(
        "Neither k3d nor minikube found in PATH. "
        "Install one to use ephemeral cluster lifecycle."
    )


def _cluster_name(task_id: str) -> str:
    return f"factory-sim-{task_id}"


def _kubeconfig_path(task_id: str) -> str:
    return f"/tmp/{_cluster_name(task_id)}.kubeconfig"


def provision(task_id: str) -> str:
    provider = _which_provider()
    cluster = _cluster_name(task_id)
    kubeconfig = _kubeconfig_path(task_id)

    emit_phase_event("PROVISION", "started", {"provider": provider, "task_id": task_id})

    try:
        if provider == "k3d":
            subprocess.run(
                [
                    "k3d", "cluster", "create", cluster,
                    "--agents", "0",
                    "--no-lb",
                    "--k3s-arg", "--disable=traefik@server:0",
                ],
                capture_output=True,
                timeout=PROVISION_TIMEOUT,
                check=True,
            )
            kc_result = subprocess.run(
                ["k3d", "kubeconfig", "get", cluster],
                capture_output=True,
                timeout=10,
                check=True,
            )
            Path(kubeconfig).write_bytes(kc_result.stdout)
        else:
            subprocess.run(
                [
                    "minikube", "start",
                    "--profile", cluster,
                    "--driver", "docker",
                    "--memory", "1024",
                    "--cpus", "1",
                ],
                capture_output=True,
                timeout=90,
                check=True,
            )
            result = subprocess.run(
                ["minikube", "profile", "list", "-o", "json"],
                capture_output=True,
                timeout=10,
                check=True,
            )
            Path(kubeconfig).write_text(
                subprocess.run(
                    ["minikube", "kubectl", "--profile", cluster, "--", "config", "view", "--flatten"],
                    capture_output=True,
                    timeout=10,
                    check=True,
                ).stdout.decode()
            )
    except subprocess.TimeoutExpired as exc:
        emit_phase_event("PROVISION", "timeout", {"provider": provider, "task_id": task_id})
        raise RuntimeError(f"Cluster provision timed out after {exc.timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        emit_phase_event(
            "PROVISION", "error",
            {"provider": provider, "stderr": (exc.stderr or b"").decode()[:500]},
        )
        raise RuntimeError(
            f"Cluster provision failed: {(exc.stderr or b'').decode()[:500]}"
        ) from exc

    emit_phase_event("PROVISION", "completed", {"provider": provider, "kubeconfig": kubeconfig})
    return kubeconfig


def apply_snapshot(kubeconfig: str, manifests: list[dict]) -> dict:
    emit_phase_event("APPLY", "started", {"manifest_count": len(manifests)})

    by_namespace: dict[str, list[dict]] = {}
    for m in manifests:
        ns = m.get("metadata", {}).get("namespace", "default")
        by_namespace.setdefault(ns, []).append(m)

    applied = 0
    failed = 0
    errors: list[str] = []

    for ns in by_namespace:
        if ns not in _SYSTEM_NAMESPACES:
            try:
                subprocess.run(
                    ["kubectl", "create", "namespace", ns, "--kubeconfig", kubeconfig],
                    capture_output=True,
                    timeout=APPLY_TIMEOUT,
                    check=True,
                )
            except subprocess.CalledProcessError:
                pass

    for ns, ns_manifests in by_namespace.items():
        for manifest in ns_manifests:
            try:
                manifest_json = json.dumps(manifest)
                subprocess.run(
                    [
                        "kubectl", "apply",
                        "--kubeconfig", kubeconfig,
                        "-f", "-",
                    ],
                    input=manifest_json.encode(),
                    capture_output=True,
                    timeout=APPLY_TIMEOUT,
                    check=True,
                )
                applied += 1
            except subprocess.CalledProcessError as exc:
                failed += 1
                errors.append(
                    f"{manifest.get('kind', '?')}/{manifest.get('metadata', {}).get('name', '?')}: "
                    f"{(exc.stderr or b'').decode()[:200]}"
                )
            except subprocess.TimeoutExpired:
                failed += 1
                errors.append(
                    f"{manifest.get('kind', '?')}/{manifest.get('metadata', {}).get('name', '?')}: timeout"
                )

    result = {"applied": applied, "failed": failed, "errors": errors}
    emit_phase_event("APPLY", "completed", result)
    return result


def teardown(task_id: str) -> None:
    emit_phase_event("TEARDOWN", "started", {"task_id": task_id})

    provider = _which_provider()
    cluster = _cluster_name(task_id)

    try:
        if provider == "k3d":
            subprocess.run(
                ["k3d", "cluster", "delete", cluster],
                capture_output=True,
                timeout=TEARDOWN_TIMEOUT,
                check=True,
            )
        else:
            subprocess.run(
                ["minikube", "delete", "--profile", cluster],
                capture_output=True,
                timeout=TEARDOWN_TIMEOUT,
                check=True,
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    kubeconfig = Path(_kubeconfig_path(task_id))
    if kubeconfig.exists():
        kubeconfig.unlink()

    emit_phase_event("TEARDOWN", "completed", {"task_id": task_id})


def reset(task_id: str, manifests: list[dict]) -> dict:
    emit_phase_event("APPLY", "reset_started", {"task_id": task_id})

    kubeconfig = _kubeconfig_path(task_id)

    try:
        result = subprocess.run(
            [
                "kubectl", "get", "namespaces",
                "--kubeconfig", kubeconfig,
                "-o", "jsonpath={.items[*].metadata.name}",
            ],
            capture_output=True,
            timeout=APPLY_TIMEOUT,
            check=True,
        )
        all_ns = result.stdout.decode().split()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        all_ns = []

    for ns in all_ns:
        if ns not in _SYSTEM_NAMESPACES:
            try:
                subprocess.run(
                    ["kubectl", "delete", "namespace", ns, "--kubeconfig", kubeconfig],
                    capture_output=True,
                    timeout=APPLY_TIMEOUT,
                    check=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass

    return apply_snapshot(kubeconfig, manifests)
