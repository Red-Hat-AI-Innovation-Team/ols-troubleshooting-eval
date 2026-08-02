from __future__ import annotations

import time
import uuid
from enum import Enum

from ols_eval.models import ProposedFix, Reproducibility, SimulationVerdict, Verdict
from ols_eval.telemetry import emit_phase_event


class PipelinePhase(str, Enum):
    SNAPSHOT = "SNAPSHOT"
    PROVISION = "PROVISION"
    APPLY = "APPLY"
    FIX = "FIX"
    VERIFY = "VERIFY"
    TEARDOWN = "TEARDOWN"


def run_pipeline(
    proposed_fix: ProposedFix,
    target_namespaces: list[str] | None = None,
    target_kubeconfig: str | None = None,
    reproducibility: str = "none",
) -> SimulationVerdict:
    from ols_eval import cluster, executor, healthcheck, sanitize, snapshot, verdict

    task_id = uuid.uuid4().hex[:12]
    namespaces = target_namespaces or ["default"]
    start_ms = _now_ms()
    ephemeral_kubeconfig: str | None = None

    try:
        emit_phase_event(PipelinePhase.SNAPSHOT, "started", {"namespaces": namespaces})
        if target_kubeconfig:
            raw_manifests: list[dict] = []
            for ns in namespaces:
                raw_manifests.extend(snapshot.snapshot_namespace(target_kubeconfig, ns))
            sanitized = sanitize.sanitize_manifests(raw_manifests)
        else:
            sanitized = []
        emit_phase_event(PipelinePhase.SNAPSHOT, "completed", {"manifest_count": len(sanitized)})

        emit_phase_event(PipelinePhase.PROVISION, "started", {"task_id": task_id})
        try:
            ephemeral_kubeconfig = cluster.provision(task_id)
        except RuntimeError as exc:
            emit_phase_event(PipelinePhase.PROVISION, "error", {"error": str(exc)})
            return _error_verdict(str(exc), _elapsed_ms(start_ms))
        emit_phase_event(PipelinePhase.PROVISION, "completed", {"kubeconfig": ephemeral_kubeconfig})

        emit_phase_event(PipelinePhase.APPLY, "started", {"manifest_count": len(sanitized)})
        apply_result = cluster.apply_snapshot(ephemeral_kubeconfig, sanitized)
        emit_phase_event(PipelinePhase.APPLY, "completed", apply_result)

        before_state = healthcheck.capture_pod_states(ephemeral_kubeconfig, namespaces)

        emit_phase_event(PipelinePhase.FIX, "started", {"command_count": len(proposed_fix.commands)})
        execution_result = executor.execute_fix(ephemeral_kubeconfig, proposed_fix)
        emit_phase_event(PipelinePhase.FIX, "completed", {
            "manifests_applied": execution_result["manifests_applied"],
        })

        emit_phase_event(PipelinePhase.VERIFY, "started", {"namespaces": namespaces})
        health_result = healthcheck.verify(
            ephemeral_kubeconfig,
            namespaces,
            before_state,
            proposed_fix.expected_outcomes,
        )
        emit_phase_event(PipelinePhase.VERIFY, "completed", {"passed": health_result["passed"]})

        topology_match = _compute_topology_match(sanitized, health_result.get("after_state", {}))

        return verdict.generate_verdict(
            health_result=health_result,
            execution_result=execution_result,
            topology_match=topology_match,
            reproducibility=reproducibility,
            execution_time_ms=_elapsed_ms(start_ms),
        )

    except Exception as exc:
        emit_phase_event("PIPELINE", "error", {"error": str(exc)})
        return _error_verdict(str(exc), _elapsed_ms(start_ms))

    finally:
        emit_phase_event(PipelinePhase.TEARDOWN, "started", {"task_id": task_id})
        try:
            cluster.teardown(task_id)
        except Exception:
            pass
        emit_phase_event(PipelinePhase.TEARDOWN, "completed", {"task_id": task_id})


def _error_verdict(error: str, execution_time_ms: int) -> SimulationVerdict:
    return SimulationVerdict(
        verdict=Verdict.FAILED,
        evidence={
            "before_state": {},
            "after_state": {},
            "topology_match": 0.0,
            "logs": [f"Pipeline error: {error}"],
        },
        execution_time_ms=execution_time_ms,
        scenario_reproducibility=Reproducibility.NONE,
    )


def _compute_topology_match(expected_manifests: list[dict], after_state: dict) -> float:
    if not expected_manifests:
        return 0.0

    expected_pods = set()
    for m in expected_manifests:
        kind = m.get("kind", "")
        name = m.get("metadata", {}).get("name", "")
        if kind in ("Deployment", "StatefulSet", "Pod") and name:
            expected_pods.add(name)

    if not expected_pods:
        return 1.0

    found = 0
    for ns_pods in after_state.values():
        for pod in ns_pods:
            pod_name = pod.get("name", "")
            for expected in expected_pods:
                if pod_name.startswith(expected):
                    found += 1
                    break

    return min(found / len(expected_pods), 1.0)


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _elapsed_ms(start_ms: int) -> int:
    return _now_ms() - start_ms
