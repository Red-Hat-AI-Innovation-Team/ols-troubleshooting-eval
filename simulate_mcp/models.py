from enum import Enum

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    FIXED_HIGH_CONFIDENCE = "FIXED_HIGH_CONFIDENCE"
    FIXED_STRUCTURAL_ONLY = "FIXED_STRUCTURAL_ONLY"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    REGRESSION = "REGRESSION"


class Reproducibility(str, Enum):
    FULL = "full"
    STRUCTURAL = "structural"
    NONE = "none"


class ProposedFix(BaseModel):
    commands: list[str]
    manifests: list[dict]
    fault_description: str
    expected_outcomes: dict | None = None


class SimulationVerdict(BaseModel):
    verdict: Verdict
    evidence: dict = Field(default_factory=lambda: {
        "before_state": {},
        "after_state": {},
        "topology_match": 0.0,
        "logs": [],
    })
    execution_time_ms: int = 0
    cluster_info: dict = Field(default_factory=dict)
    scenario_reproducibility: Reproducibility = Reproducibility.NONE


class SimulateRequest(BaseModel):
    query: str
    proposed_fix: ProposedFix
    target_namespaces: list[str] | None = None
