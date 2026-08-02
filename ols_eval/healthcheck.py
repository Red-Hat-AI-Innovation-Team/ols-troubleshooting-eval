from __future__ import annotations

import json
import subprocess
import time

from ols_eval.telemetry import emit_phase_event

POLL_TIMEOUT = 30
POLL_INTERVAL = 5
KUBECTL_TIMEOUT = 10


def capture_pod_states(kubeconfig: str, namespaces: list[str]) -> dict:
    result: dict[str, list[dict]] = {}
    for ns in namespaces:
        result[ns] = _get_pods_in_namespace(kubeconfig, ns)
    return result


def _get_pods_in_namespace(kubeconfig: str, namespace: str) -> list[dict]:
    try:
        proc = subprocess.run(
            [
                "kubectl", "get", "pods",
                "-n", namespace,
                "-o", "json",
                "--kubeconfig", kubeconfig,
            ],
            capture_output=True,
            timeout=KUBECTL_TIMEOUT,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []

    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []

    pods = []
    for item in data.get("items", []):
        pods.append(_parse_pod(item))
    return pods


def _parse_pod(item: dict) -> dict:
    metadata = item.get("metadata", {})
    status = item.get("status", {})

    container_statuses = status.get("containerStatuses", [])
    ready = all(cs.get("ready", False) for cs in container_statuses) if container_statuses else False
    restart_count = sum(cs.get("restartCount", 0) for cs in container_statuses)

    return {
        "name": metadata.get("name", ""),
        "status": status.get("phase", "Unknown"),
        "ready": ready,
        "restart_count": restart_count,
    }


def verify(
    kubeconfig: str,
    namespaces: list[str],
    before_state: dict,
    expected_outcomes: dict | None = None,
) -> dict:
    emit_phase_event("VERIFY", "started", {"namespaces": namespaces})

    checks: list[dict] = []
    after_state: dict = {}
    deadline = time.monotonic() + POLL_TIMEOUT

    while time.monotonic() < deadline:
        after_state = capture_pod_states(kubeconfig, namespaces)
        checks = _compare_states(before_state, after_state)

        if all(c["passed"] for c in checks):
            break

        time.sleep(POLL_INTERVAL)

    if not checks:
        after_state = capture_pod_states(kubeconfig, namespaces)
        checks = _compare_states(before_state, after_state)

    passed = all(c["passed"] for c in checks) if checks else False

    result = {
        "passed": passed,
        "before_state": before_state,
        "after_state": after_state,
        "checks": checks,
    }
    emit_phase_event("VERIFY", "completed", {"passed": passed, "check_count": len(checks)})
    return result


def _compare_states(before: dict, after: dict) -> list[dict]:
    checks: list[dict] = []

    all_namespaces = set(before.keys()) | set(after.keys())
    for ns in sorted(all_namespaces):
        before_pods = {p["name"]: p for p in before.get(ns, [])}
        after_pods = {p["name"]: p for p in after.get(ns, [])}

        for pod_name in sorted(set(before_pods.keys()) | set(after_pods.keys())):
            bp = before_pods.get(pod_name)
            ap = after_pods.get(pod_name)

            if bp and ap:
                checks.append(_check_status_transition(ns, pod_name, bp, ap))
                checks.append(_check_readiness(ns, pod_name, ap))
                checks.append(_check_restart_stability(ns, pod_name, bp, ap))
            elif ap and not bp:
                checks.append({
                    "name": f"{ns}/{pod_name}: new pod appeared",
                    "passed": ap["status"] == "Running",
                    "detail": f"status={ap['status']}",
                })

    return checks


def _check_status_transition(ns: str, pod_name: str, before: dict, after: dict) -> dict:
    bad_states = {"CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull"}
    before_bad = before["status"] in bad_states
    after_bad = after["status"] in bad_states
    after_running = after["status"] == "Running"

    if before_bad and after_running:
        return {
            "name": f"{ns}/{pod_name}: status transition",
            "passed": True,
            "detail": f"{before['status']} -> {after['status']}",
        }
    elif before_bad and after_bad:
        return {
            "name": f"{ns}/{pod_name}: status transition",
            "passed": False,
            "detail": f"still in bad state: {after['status']}",
        }
    elif not before_bad and after_bad:
        return {
            "name": f"{ns}/{pod_name}: status transition",
            "passed": False,
            "detail": f"regressed: {before['status']} -> {after['status']}",
        }
    else:
        return {
            "name": f"{ns}/{pod_name}: status transition",
            "passed": True,
            "detail": f"{before['status']} -> {after['status']}",
        }


def _check_readiness(ns: str, pod_name: str, after: dict) -> dict:
    return {
        "name": f"{ns}/{pod_name}: readiness",
        "passed": after["ready"],
        "detail": f"ready={after['ready']}",
    }


def _check_restart_stability(ns: str, pod_name: str, before: dict, after: dict) -> dict:
    delta = after["restart_count"] - before["restart_count"]
    return {
        "name": f"{ns}/{pod_name}: restart stability",
        "passed": delta <= 0,
        "detail": f"restarts: {before['restart_count']} -> {after['restart_count']} (delta={delta})",
    }
