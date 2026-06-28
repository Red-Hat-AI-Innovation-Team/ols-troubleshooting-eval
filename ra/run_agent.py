"""Run a single-turn troubleshooting agent loop against the mock MCP tools.

Usage:
    uv run python run_agent.py
    uv run python run_agent.py "Why is my cluster slow?"
"""

import json
import sys
from pathlib import Path

import psycopg2
from anthropic import AnthropicVertex

from mock_tools import TOOLS, call_tool

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_DSN = "host=127.0.0.1 port=5433 dbname=openshift_cluster user=postgres"
MODEL = "claude-opus-4-6@default"
MAX_TURNS = 20
THINKING_BUDGET = 10_000

DEFAULT_QUERY = (
    "We're seeing degraded performance on our ecommerce platform and some pods "
    "aren't coming up. Can you investigate what's going on?"
)

SYSTEM_PROMPT = """\
You are an OpenShift/Kubernetes troubleshooting agent. You have access to MCP \
tools that let you inspect a live cluster: list pods, read logs, check events, \
query Prometheus metrics, inspect alerts, exec into containers, and more.

Your job is to investigate the cluster state using these tools and provide a \
clear, evidence-based diagnosis. Do NOT guess or give generic advice — use the \
tools to gather real data and base your answer on what you find.

Be thorough but efficient. Start broad (check alerts, pod status, events) then \
drill into specific issues you discover."""

# ---------------------------------------------------------------------------
# Load tool definitions
# ---------------------------------------------------------------------------

STRIP_PARAMS = {"context"}  # not supported by mock tools

# Tools in TOOLS registry but not in raw_tool_defs.json — add minimal defs
EXTRA_TOOL_DEFS = [
    {
        "name": "projects_list",
        "description": "List all OpenShift projects (namespaces with display names) in the cluster",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "targets_list",
        "description": "List all Prometheus scrape targets and their status",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def load_tool_defs() -> list[dict]:
    """Load tool defs from raw_tool_defs.json, convert to Anthropic format."""
    raw = json.loads((Path(__file__).parent / "raw_tool_defs.json").read_text())

    tools: list[dict] = []
    for _server, section in raw.items():
        if not isinstance(section, dict) or "tools" not in section:
            continue
        for t in section["tools"]:
            fn = t["function"]
            name = fn["name"]
            if name not in TOOLS:
                continue

            # Convert OpenAI format -> Anthropic format
            params = fn.get("parameters", {"type": "object", "properties": {}})
            # Strip unsupported params
            props = {k: v for k, v in params.get("properties", {}).items() if k not in STRIP_PARAMS}
            required = [r for r in params.get("required", []) if r not in STRIP_PARAMS]

            input_schema = {"type": "object", "properties": props}
            if required:
                input_schema["required"] = required

            tools.append({
                "name": name,
                "description": fn.get("description", ""),
                "input_schema": input_schema,
            })

    # Add extras not in raw defs
    for extra in EXTRA_TOOL_DEFS:
        if extra["name"] not in {t["name"] for t in tools}:
            tools.append(extra)

    return tools


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def run_agent(query: str) -> str:
    """Run the agent loop and return the final text answer."""
    conn = psycopg2.connect(DB_DSN)
    client = AnthropicVertex()
    tool_defs = load_tool_defs()

    messages: list[dict] = [{"role": "user", "content": query}]

    print(f"User: {query}\n")
    print("=" * 60)

    for turn in range(MAX_TURNS):
        print(f"\n--- Turn {turn + 1} ---")

        with client.messages.stream(
            model=MODEL,
            max_tokens=16_000,
            thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET},
            system=SYSTEM_PROMPT,
            tools=tool_defs,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        # Collect tool_use blocks and text blocks
        tool_uses: list = []
        text_parts: list[str] = []

        for block in response.content:
            if block.type == "tool_use":
                tool_uses.append(block)
            elif block.type == "text":
                text_parts.append(block.text)

        # Print tool calls
        for tu in tool_uses:
            params_str = json.dumps(tu.input, separators=(",", ":")) if tu.input else "{}"
            print(f"  -> {tu.name}({params_str})")

        # If no tool calls, we're done
        if not tool_uses:
            final_answer = "\n".join(text_parts)
            conn.close()
            return final_answer

        # Execute tools and build next messages
        messages.append({"role": "assistant", "content": response.content})

        tool_results: list[dict] = []
        for tu in tool_uses:
            # Strip unsupported params before dispatching
            clean_params = {k: v for k, v in (tu.input or {}).items() if k not in STRIP_PARAMS}
            result = call_tool(conn, tu.name, clean_params)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    # Safety: hit max turns
    conn.close()
    return "[Agent hit max turns without producing a final answer]"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    query = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
    answer = run_agent(query)
    print("\n" + "=" * 60)
    print("FINAL ANSWER:")
    print("=" * 60)
    print(answer)
