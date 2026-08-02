"""Bridge between OLS fix proposals and the simulate workflow skill."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
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


WORKFLOW_PHASES = [
    ("snapshot", "builder", "Export and sanitize manifests from the target cluster. "
     "Read .factory/simulate/analysis.json for namespaces and resource types. "
     "Write snapshot-report.md."),
    ("provision", "builder", "Provision an ephemeral cluster. "
     "Read .factory/simulate/analysis.json for cluster_type and microshift_port. "
     "Write provision-report.md."),
    ("apply-baseline", "builder", "Apply sanitized manifests to the ephemeral cluster. "
     "Read manifests from .factory/simulate/manifests/. "
     "Write apply-report.md."),
    ("apply-fix", "builder", "Apply proposed fix commands to the ephemeral cluster. "
     "Read .factory/simulate/task.json for fix_commands. "
     "Write fix-report.md."),
    ("verify", "health_checker", "Verify the structural topology of the ephemeral cluster. "
     "Read apply-report.md and fix-report.md. "
     "Write verify-report.md."),
]


def _invoke_factory_agent(
    role: str,
    task_description: str,
    project_path: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "factory", "agent", role,
        "--task", task_description,
        "--project", project_path,
        "--timeout", str(timeout),
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout + 30,
    )


def _parse_verify_report(report_text: str) -> tuple[Verdict, float]:
    score_match = re.search(
        r"(?:structural\s+health\s+)?score[:\s]+([0-9]+(?:\.[0-9]+)?)",
        report_text,
        re.IGNORECASE,
    )
    score = float(score_match.group(1)) if score_match else 0.0

    if score >= 1.0:
        return Verdict.FIXED_HIGH_CONFIDENCE, score
    if score >= 0.5:
        return Verdict.PARTIAL, score
    return Verdict.FAILED, score


def _extract_failed_checks(report_text: str) -> list[str]:
    failed = []
    for line in report_text.splitlines():
        lower = line.lower()
        if any(kw in lower for kw in ("fail", "missing", "not found", "error", "mismatch")):
            failed.append(line.strip())
    return failed[:20]


def _extract_log_entries(fix_report_text: str) -> list[str]:
    logs: list[str] = []
    for line in fix_report_text.splitlines():
        lower = line.lower()
        if any(kw in lower for kw in (
            "stderr:", "error", "crashloopbackoff", "oomkilled",
            "imagepullbackoff", "exit code:", "failed",
        )):
            logs.append(line.strip())
    return logs[:20]


def run_simulation(project_path: str, timeout: int = 600) -> dict[str, Any]:
    """Execute the simulate workflow phases and return a verdict dict."""
    simulate_dir = Path(project_path) / ".factory" / "simulate"
    start_ms = int(time.monotonic() * 1000)
    phase_results: dict[str, dict[str, Any]] = {}
    per_phase_timeout = timeout // len(WORKFLOW_PHASES)

    for phase_name, role, task_desc in WORKFLOW_PHASES:
        full_task = (
            f"Simulate workflow phase: {phase_name}. {task_desc}\n"
            f"Read: .factory/simulate/task.json\n"
            f"Write output to: .factory/simulate/{phase_name}-report.md"
        )
        try:
            result = _invoke_factory_agent(
                role=role,
                task_description=full_task,
                project_path=project_path,
                timeout=per_phase_timeout,
            )
            phase_results[phase_name] = {
                "returncode": result.returncode,
                "stdout": result.stdout[-2000:] if result.stdout else "",
                "stderr": result.stderr[-2000:] if result.stderr else "",
            }
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            phase_results[phase_name] = {
                "returncode": -1,
                "stdout": "",
                "stderr": str(exc),
            }
            break

    elapsed_ms = int(time.monotonic() * 1000) - start_ms

    verify_report = simulate_dir / "verify-report.md"
    fix_report = simulate_dir / "fix-report.md"

    if verify_report.exists():
        report_text = verify_report.read_text()
        verdict_val, topology_score = _parse_verify_report(report_text)
        failed_checks = _extract_failed_checks(report_text)
    else:
        verdict_val = Verdict.FAILED
        topology_score = 0.0
        failed_checks = ["verify-report.md not found — verification phase did not complete"]

    log_entries: list[str] = []
    fix_report_text = ""
    if fix_report.exists():
        fix_report_text = fix_report.read_text()
        log_entries = _extract_log_entries(fix_report_text)

    reproducibility = Reproducibility.NONE
    if verdict_val == Verdict.FIXED_HIGH_CONFIDENCE:
        reproducibility = Reproducibility.FULL
    elif verdict_val == Verdict.PARTIAL:
        reproducibility = Reproducibility.STRUCTURAL

    verdict = SimulationVerdict(
        verdict=verdict_val,
        evidence={
            "before_state": {},
            "after_state": {},
            "topology_match": topology_score,
            "logs": log_entries,
            "failed_checks": failed_checks,
            "fix_report": fix_report_text[:4000] if fix_report_text else "",
            "phase_results": phase_results,
        },
        execution_time_ms=elapsed_ms,
        cluster_info={},
        scenario_reproducibility=reproducibility,
    )

    verdict_path = simulate_dir / "verdict.json"
    verdict_dict = verdict.model_dump()
    verdict_path.write_text(json.dumps(verdict_dict, indent=2))

    return verdict_dict


def format_failure_context(verdict: dict[str, Any]) -> str:
    """Convert a FAILED/PARTIAL verdict into natural-language failure context."""
    status = verdict.get("verdict", "FAILED")
    evidence = verdict.get("evidence", {})

    lines = [f"## Simulation Result: {status}", ""]

    if status in ("FIXED_HIGH_CONFIDENCE", "FIXED_STRUCTURAL_ONLY"):
        lines.append("The proposed fix was validated successfully on the ephemeral cluster.")
        return "\n".join(lines)

    if status == "PARTIAL":
        lines.append("The proposed fix **partially** resolved the issue on the ephemeral cluster.")
    else:
        lines.append("The proposed fix did **not** resolve the issue on the ephemeral cluster.")
    lines.append("")

    topology_match = evidence.get("topology_match", 0.0)
    lines.append(f"- Topology match score: {topology_match}")

    failed_checks = evidence.get("failed_checks", [])
    if failed_checks:
        lines.append("")
        lines.append("### Verification Checks That Failed")
        for check in failed_checks[:10]:
            lines.append(f"- {check}")

    logs = evidence.get("logs", [])
    if logs:
        lines.append("")
        lines.append("### Relevant Pod Logs and Events")
        for log_entry in logs[:10]:
            lines.append(f"- {log_entry}")

    fix_report = evidence.get("fix_report", "")
    if fix_report:
        lines.append("")
        lines.append("### Fix Execution Details")
        lines.append(fix_report[:2000])

    execution_time = verdict.get("execution_time_ms", 0)
    if execution_time:
        lines.append("")
        lines.append(f"Simulation completed in {execution_time}ms.")

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
