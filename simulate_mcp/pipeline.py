from enum import Enum

from simulate_mcp.models import ProposedFix, Reproducibility, SimulationVerdict, Verdict
from simulate_mcp.telemetry import emit_phase_event


class PipelinePhase(str, Enum):
    SNAPSHOT = "SNAPSHOT"
    PROVISION = "PROVISION"
    APPLY = "APPLY"
    FIX = "FIX"
    VERIFY = "VERIFY"
    TEARDOWN = "TEARDOWN"


# Stub — full implementation in Phases 2-4
def run_pipeline(
    proposed_fix: ProposedFix,
    target_namespaces: list[str] | None = None,
) -> SimulationVerdict:
    emit_phase_event(PipelinePhase.SNAPSHOT, "skipped", {"reason": "stub"})

    return SimulationVerdict(
        verdict=Verdict.FAILED,
        evidence={
            "before_state": {},
            "after_state": {},
            "topology_match": 0.0,
            "logs": ["Pipeline not yet implemented — stub returning FAILED"],
        },
        execution_time_ms=0,
        cluster_info={},
        scenario_reproducibility=Reproducibility.NONE,
    )
