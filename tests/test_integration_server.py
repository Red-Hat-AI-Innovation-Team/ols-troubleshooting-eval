import pytest

from simulate_mcp.models import ProposedFix, SimulationVerdict


def test_simulate_mcp_package_importable():
    import simulate_mcp

    assert hasattr(simulate_mcp, "__init__")


def test_server_module_has_mcp_instance():
    server = pytest.importorskip(
        "simulate_mcp.server",
        reason="simulate_mcp.server requires mcp.server.fastmcp (MCP <2.0)",
    )
    assert server.mcp.name == "factory-simulate-mcp"


def test_simulate_fix_tool_registered():
    server = pytest.importorskip(
        "simulate_mcp.server",
        reason="simulate_mcp.server requires mcp.server.fastmcp (MCP <2.0)",
    )
    tools = {name for name in server.mcp._tool_manager._tools}
    assert "simulate_fix" in tools


def test_proposed_fix_model():
    fix = ProposedFix(
        commands=["kubectl set env deploy/app DEPLOY_ENV=prod"],
        manifests=[],
        fault_description="Missing DEPLOY_ENV env var",
    )
    assert fix.commands[0].startswith("kubectl")
    assert fix.expected_outcomes is None


def test_simulation_verdict_model():
    verdict = SimulationVerdict(verdict="FAILED")
    assert verdict.verdict.value == "FAILED"
    assert verdict.execution_time_ms == 0
    assert verdict.scenario_reproducibility.value == "none"
