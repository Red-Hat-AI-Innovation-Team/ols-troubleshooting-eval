"""Scenario registry for OLS troubleshooting evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Turn:
    """A single turn in a multi-turn evaluation scenario."""

    turn_id: int
    query: str
    expected_response: str
    contexts: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    """An evaluation scenario with one or more turns."""

    scenario_id: str
    description: str
    turns: list[Turn]
    seed_path: str


SCENARIOS: dict[str, Scenario] = {
    "envvar_missing": Scenario(
        scenario_id="envvar_missing",
        description="Missing environment variable in order-fulfillment-daemon",
        turns=[
            Turn(
                turn_id=1,
                query="What is the issue with order-fulfillment-daemon?",
                expected_response="The DEPLOY_ENV environment variable is undefined or missing",
            ),
        ],
        seed_path="ra/seeds/envvar_missing.json",
    ),
    "batch_failure": Scenario(
        scenario_id="batch_failure",
        description="Job inventory-sync-validator fails to connect to database",
        turns=[
            Turn(
                turn_id=1,
                query="What is the issue with job inventory-sync-validator in namespace catalog-mgmt",
                expected_response="The inventory-sync-validator job repeatedly fails to connect to the database at prod-db:3333",
            ),
        ],
        seed_path="ra/seeds/batch_failure.json",
    ),
    "storage_binding": Scenario(
        scenario_id="storage_binding",
        description="Misconfigured PVC for memcached",
        turns=[
            Turn(
                turn_id=1,
                query="What is the issue with memcached?",
                expected_response="The PersistentVolumeClaim (PVC) is misconfigured",
            ),
        ],
        seed_path="ra/seeds/storage_binding.json",
    ),
    "namespace_pod_count": Scenario(
        scenario_id="namespace_pod_count",
        description="Count running pods in fleet-alpha namespace",
        turns=[
            Turn(
                turn_id=1,
                query="How many pods are running in the fleet-alpha namespace?",
                expected_response="There are 6 pods running in the fleet-alpha namespace (not including pods from fleet-alpha1)",
            ),
        ],
        seed_path="ra/seeds/namespace_pod_count.json",
    ),
    "scheduled_outage_detection": Scenario(
        scenario_id="scheduled_outage_detection",
        description="Detect scheduled outage in report-generator logs",
        turns=[
            Turn(
                turn_id=1,
                query=(
                    "The report-generator is processing historical records. "
                    "Analyze the provided logs for the report-generator pod to "
                    "identify any issues that occurred between 03:00 and 04:00"
                ),
                expected_response=(
                    "Report generator pod experienced temporary issues between "
                    "when it could not connect to an external API possibly due "
                    "to a maintenance window"
                ),
            ),
        ],
        seed_path="ra/seeds/scheduled_outage_detection.json",
    ),
    "periodic_failure_window": Scenario(
        scenario_id="periodic_failure_window",
        description="Identify periodic failure window in batch-processor logs",
        turns=[
            Turn(
                turn_id=1,
                query=(
                    "The batch-processor is processing historical records. "
                    "Some pod ERROR logs reported issues. Tell me when"
                ),
                expected_response=(
                    "Batch processor pod reported issues between 03:00-03:05 "
                    "when it could not connect to an external API possibly due "
                    "to a maintenance window."
                ),
                contexts=[
                    "Answer MUST include the time window of the issue (03:00-03:05)",
                ],
            ),
        ],
        seed_path="ra/seeds/periodic_failure_window.json",
    ),
    "config_drift_analysis": Scenario(
        scenario_id="config_drift_analysis",
        description="Config drift causing connection refused in gateway-proxy",
        turns=[
            Turn(
                turn_id=1,
                query=(
                    "What is causing connection refused errors in gateway-proxy "
                    "in namespace ingress-layer?"
                ),
                expected_response=(
                    "The first Connection refused errors started after a config "
                    "reload that loaded staging database settings in production"
                ),
            ),
        ],
        seed_path="ra/seeds/config_drift_analysis.json",
    ),
    "readiness_probe_diagnosis": Scenario(
        scenario_id="readiness_probe_diagnosis",
        description="Failing readiness probe on catalog-index-service",
        turns=[
            Turn(
                turn_id=1,
                query="What is the issue with catalog-index-service",
                expected_response=(
                    "The catalog-index-service pod is not ready due to a "
                    "failing readiness probe"
                ),
            ),
        ],
        seed_path="ra/seeds/readiness_probe_diagnosis.json",
    ),
    "ingress_rule_mismatch": Scenario(
        scenario_id="ingress_rule_mismatch",
        description="NetworkPolicy blocks frontend-to-backend traffic",
        turns=[
            Turn(
                turn_id=1,
                query=(
                    "Why is the web-portal getting timeouts connecting to "
                    "api-gateway in namespace platform-core?"
                ),
                expected_response=(
                    "Web-portal getting timeouts because NetworkPolicy on "
                    "api-gateway only allows ingress from pods with tier "
                    "backend label not frontend"
                ),
            ),
        ],
        seed_path="ra/seeds/ingress_rule_mismatch.json",
    ),
    "oom": Scenario(
        scenario_id="oom",
        description="OOMKilled pod due to memory leak",
        turns=[
            Turn(
                turn_id=1,
                query="Why awesome-application is in CrashLoopBackOff?",
                expected_response=(
                    "Root cause: the app container is being OOMKilled due to a "
                    "deliberate memory leak in the pod command, while the "
                    "container has a 60Mi memory limit. Deployment "
                    "awesome-application runs Python that appends ~1MB/sec forever."
                ),
                contexts=[
                    "The awesome-application is in CrashLoopBackOff due to OOM kill",
                    "The awesome-application is in out of memory due a memory leakage in the deployment command",
                    (
                        "The responses should not include hints or suggestions to "
                        "the user on what command to run and how. The preferred "
                        "response, the ones that should get a better score, are the "
                        "ones which points out an issue, and possibly also a solution "
                        "to address that"
                    ),
                ],
            ),
        ],
        seed_path="ra/seeds/oom.json",
    ),
    "wrong_networkpolicy": Scenario(
        scenario_id="wrong_networkpolicy",
        description="Multi-turn: diagnose and fix wrong NetworkPolicy",
        turns=[
            Turn(
                turn_id=1,
                query="What is the status of the frontend application",
                expected_response=(
                    "Frontend application is up and running, logs show errors "
                    "about reaching the backend-service."
                ),
                contexts=[
                    "Frontend pod logs show repeated ERROR: Connection timeout to backend-service! and wget timeouts.",
                    "TCP checks to backend pod IP and backend service IP both time out",
                ],
            ),
            Turn(
                turn_id=2,
                query="Can you spot the root cause?",
                expected_response=(
                    "The backend-network-policy block ingress from pods with "
                    "tier=frontend or app=frontend"
                ),
                contexts=[
                    "backend-service Endpoints and EndpointSlice are correct and point to the backend pod.",
                    "backend pod is running and healthy, no errors in logs.",
                    (
                        "NetworkPolicy backend-network-policy only allows ingress "
                        "to backend pods from other pods with label tier=backend, "
                        "but frontend has tier=frontend."
                    ),
                ],
            ),
            Turn(
                turn_id=3,
                query=(
                    "How can I modify the network policy to allow the frontend "
                    "to communicate with the backend?"
                ),
                expected_response=(
                    "You need to update the backend-network-policy ingress rules. "
                    "Add a new item to the from selector to include pods with the "
                    "label tier: frontend."
                ),
                contexts=[
                    "The current NetworkPolicy backend-network-policy uses a podSelector with matchLabels: tier: backend.",
                    "The ingress rule currently only allows from pods with label tier: backend.",
                    "The frontend pods are confirmed to have the label tier: frontend.",
                ],
            ),
        ],
        seed_path="ra/seeds/wrong_networkpolicy.json",
    ),
}


def get_scenario(scenario_id: str) -> Scenario:
    """Return a scenario by ID, raising KeyError if not found."""
    return SCENARIOS[scenario_id]


def list_scenarios() -> list[str]:
    """Return sorted list of all scenario IDs."""
    return sorted(SCENARIOS.keys())


def load_seed(scenario_id: str) -> dict:
    """Load seed data for a scenario from seeds/<id>.json."""
    _this_dir = Path(__file__).resolve().parent
    seed_file = Path(SCENARIOS[scenario_id].seed_path).name
    path = _this_dir.parent / "seeds" / seed_file
    with open(path) as f:
        return json.load(f)
