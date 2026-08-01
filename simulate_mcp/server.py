from mcp.server.fastmcp import FastMCP

from simulate_mcp.models import ProposedFix, SimulateRequest, SimulationVerdict
from simulate_mcp.pipeline import run_pipeline

mcp = FastMCP("factory-simulate-mcp", port=8086)


@mcp.tool()
def simulate_fix(
    commands: list[str],
    manifests: list[dict],
    fault_description: str,
    expected_outcomes: dict | None = None,
    target_namespaces: list[str] | None = None,
) -> dict:
    """Validate a proposed Kubernetes fix on an ephemeral cluster.

    Provisions a lightweight K3d cluster, replays a sanitized snapshot of the
    affected namespace, applies the proposed fix, runs health checks, and
    returns a structured verdict indicating fix confidence.
    """
    proposed_fix = ProposedFix(
        commands=commands,
        manifests=manifests,
        fault_description=fault_description,
        expected_outcomes=expected_outcomes,
    )

    verdict = run_pipeline(proposed_fix, target_namespaces)
    return verdict.model_dump()


def main() -> None:
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
