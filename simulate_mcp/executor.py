from __future__ import annotations

import subprocess
import time

from simulate_mcp.models import ProposedFix
from simulate_mcp.telemetry import emit_phase_event

COMMAND_TIMEOUT = 60
TOTAL_TIMEOUT = 120


def execute_fix(kubeconfig: str, fix: ProposedFix) -> dict:
    emit_phase_event("FIX", "started", {
        "command_count": len(fix.commands),
        "manifest_count": len(fix.manifests),
    })

    results: list[dict] = []
    manifests_applied = 0
    manifests_failed = 0
    deadline = time.monotonic() + TOTAL_TIMEOUT

    for command in fix.commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append({
                "command": command,
                "stdout": "",
                "stderr": "total timeout exceeded",
                "exit_code": -1,
            })
            continue

        timeout = min(COMMAND_TIMEOUT, remaining)
        result = _run_kubectl_command(kubeconfig, command, timeout)
        results.append(result)

    for manifest in fix.manifests:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            manifests_failed += 1
            continue

        timeout = min(COMMAND_TIMEOUT, remaining)
        ok = _apply_manifest(kubeconfig, manifest, timeout)
        if ok:
            manifests_applied += 1
        else:
            manifests_failed += 1

    outcome = {
        "commands": results,
        "manifests_applied": manifests_applied,
        "manifests_failed": manifests_failed,
    }
    emit_phase_event("FIX", "completed", {
        "commands_run": len(results),
        "manifests_applied": manifests_applied,
        "manifests_failed": manifests_failed,
    })
    return outcome


def _run_kubectl_command(kubeconfig: str, command: str, timeout: float) -> dict:
    parts = command.split()
    if parts and parts[0] == "kubectl":
        parts = parts[1:]

    cmd = ["kubectl", "--kubeconfig", kubeconfig] + parts

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
        )
        return {
            "command": command,
            "stdout": proc.stdout.decode(errors="replace"),
            "stderr": proc.stderr.decode(errors="replace"),
            "exit_code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "stdout": "",
            "stderr": f"command timed out after {timeout:.0f}s",
            "exit_code": -1,
        }


def _apply_manifest(kubeconfig: str, manifest: dict, timeout: float) -> bool:
    import json

    cmd = ["kubectl", "apply", "--kubeconfig", kubeconfig, "-f", "-"]
    try:
        subprocess.run(
            cmd,
            input=json.dumps(manifest).encode(),
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
