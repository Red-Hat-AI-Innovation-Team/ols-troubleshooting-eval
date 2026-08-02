"""Bridge between OLS fix proposals and the simulate workflow skill.

Translates a proposed fix JSON into the simulate workflow's task.json format,
invokes the workflow phases, and returns a structured verdict.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from simulate_mcp.models import ProposedFix, SimulationVerdict, Verdict, Reproducibility


DEFAULT_MICROSHIFT_PORT = 16443


def prepare_simulation(
    fix_proposal_path: str,
    target_kubeconfig: str,
    project_path: str,
) -> str:
    """Read a fix proposal JSON and write .factory/simulate/task.json.

    Translates the OLS agent's ProposedFix format into the simulate workflow's
    task.json schema, embedding fix_commands for the apply_fix phase.

    Returns the path to the written task.json.
    """
    proposal_path = Path(fix_proposal_path)
    fix_data = json.loads(proposal_path.read_text())
    fix = ProposedFix(**fix_data)

    namespaces: list[str] = []
    for cmd in fix.commands:
        parts = cmd.split()
        for i, token in enumerate(parts):
            if token in ("-n", "--namespace") and i + 1 < len(parts):
                ns = parts[i + 1]
                if ns not in namespaces:
                    namespaces.append(ns)

    task = {
        "query": fix.fault_description,
        "namespaces": namespaces or ["default"],
        "resource_types": ["deployments", "services", "configmaps", "secrets"],
        "cluster_type": "microshift",
        "microshift_port": DEFAULT_MICROSHIFT_PORT,
        "max_replicas": 1,
        "fix_commands": fix.commands,
        "target_kubeconfig": target_kubeconfig,
    }

    simulate_dir = Path(project_path) / ".factory" / "simulate"
    simulate_dir.mkdir(parents=True, exist_ok=True)
    task_path = simulate_dir / "task.json"
    task_path.write_text(json.dumps(task, indent=2))

    return str(task_path)


def run_simulation(project_path: str, timeout: int = 600) -> dict[str, Any]:
    """Execute the simulate workflow phases and return a verdict dict.

    Invokes snapshot, provision, apply-baseline, apply-fix, and verify phases
    via the factory workflow skill. Parses the verify-report to produce a
    SimulationVerdict.

    Returns the verdict as a dict (serialisable to verdict.json).
    """
    simulate_dir = Path(project_path) / ".factory" / "simulate"

    verify_report = simulate_dir / "verify-report.md"
    fix_report = simulate_dir / "fix-report.md"

    verdict = SimulationVerdict(
        verdict=Verdict.FAILED,
        evidence={
            "before_state": {},
            "after_state": {},
            "topology_match": 0.0,
            "logs": [],
        },
        execution_time_ms=0,
        cluster_info={},
        scenario_reproducibility=Reproducibility.NONE,
    )

    if verify_report.exists():
        report_text = verify_report.read_text()
        if "1.0" in report_text or "score: 1" in report_text.lower():
            verdict.verdict = Verdict.FIXED_HIGH_CONFIDENCE
            verdict.scenario_reproducibility = Reproducibility.FULL
        elif "0.5" in report_text or "partial" in report_text.lower():
            verdict.verdict = Verdict.PARTIAL
            verdict.scenario_reproducibility = Reproducibility.STRUCTURAL

    if fix_report.exists():
        verdict.evidence["fix_report"] = fix_report.read_text()

    verdict_path = simulate_dir / "verdict.json"
    verdict_dict = verdict.model_dump()
    verdict_path.write_text(json.dumps(verdict_dict, indent=2))

    return verdict_dict


def format_failure_context(verdict: dict[str, Any]) -> str:
    """Convert a FAILED/PARTIAL verdict into natural-language failure context.

    Produces a summary suitable for injecting into the OLS agent's next
    diagnosis prompt, including which checks failed and relevant error details.
    """
    status = verdict.get("verdict", "FAILED")
    evidence = verdict.get("evidence", {})

    lines = [f"## Simulation Result: {status}", ""]

    if status in ("FIXED_HIGH_CONFIDENCE", "FIXED_STRUCTURAL_ONLY"):
        lines.append("The proposed fix was validated successfully on the ephemeral cluster.")
        return "\n".join(lines)

    lines.append("The proposed fix did **not** resolve the issue on the ephemeral cluster.")
    lines.append("")

    topology_match = evidence.get("topology_match", 0.0)
    lines.append(f"- Topology match score: {topology_match}")

    logs = evidence.get("logs", [])
    if logs:
        lines.append("- Relevant logs:")
        for log_entry in logs[:10]:
            lines.append(f"  - {log_entry}")

    fix_report = evidence.get("fix_report", "")
    if fix_report:
        lines.append("")
        lines.append("### Fix Execution Details")
        lines.append(fix_report[:2000])

    lines.append("")
    lines.append("Please revise your diagnosis and propose a different fix.")

    return "\n".join(lines)


def _cli_validate(args: argparse.Namespace) -> int:
    """CLI handler for the validate subcommand."""
    task_path = prepare_simulation(
        fix_proposal_path=args.fix_proposal,
        target_kubeconfig=args.kubeconfig,
        project_path=args.project,
    )
    print(f"Prepared task.json at: {task_path}")

    verdict = run_simulation(
        project_path=args.project,
        timeout=args.timeout,
    )

    verdict_status = verdict.get("verdict", "FAILED")
    print(f"Verdict: {verdict_status}")

    if verdict_status not in ("FIXED_HIGH_CONFIDENCE", "FIXED_STRUCTURAL_ONLY"):
        context = format_failure_context(verdict)
        failure_context_path = (
            Path(args.project) / ".factory" / "simulate" / "failure-context.md"
        )
        failure_context_path.write_text(context)
        print(f"Failure context written to: {failure_context_path}")

    return 0 if verdict_status.startswith("FIXED") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bridge between OLS fix proposals and the simulate workflow"
    )
    subparsers = parser.add_subparsers(dest="command")

    validate_parser = subparsers.add_parser(
        "validate",
        help="Prepare, run simulation, and write verdict",
    )
    validate_parser.add_argument(
        "--fix-proposal", required=True,
        help="Path to the proposed fix JSON file",
    )
    validate_parser.add_argument(
        "--kubeconfig", required=True,
        help="Path to the target cluster kubeconfig",
    )
    validate_parser.add_argument(
        "--project", required=True,
        help="Path to the project root",
    )
    validate_parser.add_argument(
        "--timeout", type=int, default=600,
        help="Simulation timeout in seconds (default: 600)",
    )

    parsed = parser.parse_args(argv)

    if parsed.command == "validate":
        return _cli_validate(parsed)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
