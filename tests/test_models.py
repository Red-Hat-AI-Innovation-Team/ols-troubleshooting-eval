import pytest
from pydantic import ValidationError

from ols_eval.models import (
    ProposedFix,
    Reproducibility,
    SimulateRequest,
    SimulationVerdict,
    Verdict,
)


class TestVerdict:
    def test_all_values_present(self):
        expected = {
            "FIXED_HIGH_CONFIDENCE",
            "FIXED_STRUCTURAL_ONLY",
            "PARTIAL",
            "FAILED",
            "REGRESSION",
        }
        assert {v.value for v in Verdict} == expected

    def test_string_enum(self):
        assert Verdict.FAILED == "FAILED"
        assert isinstance(Verdict.FIXED_HIGH_CONFIDENCE, str)


class TestReproducibility:
    def test_all_values_present(self):
        assert {r.value for r in Reproducibility} == {"full", "structural", "none"}


class TestProposedFix:
    def test_required_fields(self):
        fix = ProposedFix(
            commands=["kubectl rollout restart deployment/app"],
            manifests=[{"apiVersion": "v1", "kind": "ConfigMap"}],
            fault_description="Pod in CrashLoopBackOff due to missing env var",
        )
        assert fix.commands == ["kubectl rollout restart deployment/app"]
        assert fix.fault_description.startswith("Pod in")
        assert fix.expected_outcomes is None

    def test_missing_commands_raises(self):
        with pytest.raises(ValidationError):
            ProposedFix(
                manifests=[],
                fault_description="test",
            )

    def test_missing_fault_description_raises(self):
        with pytest.raises(ValidationError):
            ProposedFix(
                commands=[],
                manifests=[],
            )

    def test_missing_manifests_raises(self):
        with pytest.raises(ValidationError):
            ProposedFix(
                commands=[],
                fault_description="test",
            )

    def test_round_trip_serialization(self):
        fix = ProposedFix(
            commands=["kubectl apply -f fix.yaml"],
            manifests=[{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web"}}],
            fault_description="Deployment missing readiness probe",
            expected_outcomes={"pod_status": "Running", "restart_count": 0},
        )
        data = fix.model_dump()
        restored = ProposedFix.model_validate(data)
        assert restored == fix

    def test_json_round_trip(self):
        fix = ProposedFix(
            commands=["cmd1", "cmd2"],
            manifests=[{}],
            fault_description="fault",
        )
        json_str = fix.model_dump_json()
        restored = ProposedFix.model_validate_json(json_str)
        assert restored == fix


class TestSimulationVerdict:
    def test_defaults(self):
        verdict = SimulationVerdict(verdict=Verdict.FAILED)
        assert verdict.execution_time_ms == 0
        assert verdict.cluster_info == {}
        assert verdict.scenario_reproducibility == Reproducibility.NONE
        assert "before_state" in verdict.evidence
        assert "after_state" in verdict.evidence
        assert "topology_match" in verdict.evidence
        assert "logs" in verdict.evidence

    def test_round_trip_serialization(self):
        verdict = SimulationVerdict(
            verdict=Verdict.FIXED_HIGH_CONFIDENCE,
            evidence={
                "before_state": {"pod": "CrashLoopBackOff"},
                "after_state": {"pod": "Running"},
                "topology_match": 1.0,
                "logs": ["fix applied successfully"],
            },
            execution_time_ms=45000,
            cluster_info={"name": "factory-sim-test", "provider": "k3d"},
            scenario_reproducibility=Reproducibility.FULL,
        )
        data = verdict.model_dump()
        restored = SimulationVerdict.model_validate(data)
        assert restored == verdict
        assert restored.verdict == Verdict.FIXED_HIGH_CONFIDENCE

    def test_json_round_trip(self):
        verdict = SimulationVerdict(
            verdict=Verdict.PARTIAL,
            execution_time_ms=12000,
        )
        json_str = verdict.model_dump_json()
        restored = SimulationVerdict.model_validate_json(json_str)
        assert restored == verdict


class TestSimulateRequest:
    def test_full_request(self):
        req = SimulateRequest(
            query="Why is my pod crashing?",
            proposed_fix=ProposedFix(
                commands=["kubectl set env deployment/app DEPLOY_ENV=prod"],
                manifests=[],
                fault_description="Missing DEPLOY_ENV environment variable",
            ),
            target_namespaces=["default", "monitoring"],
        )
        assert req.target_namespaces == ["default", "monitoring"]
        assert req.proposed_fix.fault_description.startswith("Missing")

    def test_optional_namespaces(self):
        req = SimulateRequest(
            query="test query",
            proposed_fix=ProposedFix(
                commands=[],
                manifests=[],
                fault_description="test",
            ),
        )
        assert req.target_namespaces is None

    def test_round_trip(self):
        req = SimulateRequest(
            query="diagnose this",
            proposed_fix=ProposedFix(
                commands=["kubectl delete pod/bad"],
                manifests=[{"kind": "Pod"}],
                fault_description="stuck pod",
                expected_outcomes={"status": "Running"},
            ),
            target_namespaces=["app-ns"],
        )
        data = req.model_dump()
        restored = SimulateRequest.model_validate(data)
        assert restored == req
