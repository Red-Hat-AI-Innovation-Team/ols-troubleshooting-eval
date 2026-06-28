"""Run a troubleshooting agent loop against the mock MCP tools.

Usage:
    uv run python run_agent.py
    uv run python run_agent.py "Why is my cluster slow?"
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psycopg2
from anthropic import AnthropicVertex

from mock_tools import TOOLS, call_tool

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_DSN = "host=127.0.0.1 port=5433 dbname=openshift_cluster user=postgres"

SEED_DATA_PATH = Path(__file__).parent / "seed_data.json"

QUESTION_GEN_SYSTEM_PROMPT = """\
You have complete knowledge of an OpenShift/Kubernetes cluster's internal state. \
Below is the full database backing this cluster — every table, every row.

CLUSTER DATABASE:
{seed_data}

You are role-playing as a mid-level SRE who just got paged or noticed something \
wrong on a dashboard. You do NOT have direct database access — you can only see \
what a real operator would see: dashboards, pager alerts, user complaints, \
kubectl output you glanced at.

Generate a single realistic troubleshooting question. Rules:
- Sound like a real human typing in Slack or a chat window — casual, not a report
- Mention only 1-3 symptoms you'd actually notice first (NOT a complete inventory)
- Leave the root cause for the agent to discover
- Do NOT enumerate every affected pod/namespace/node — pick what stands out most
- Do NOT mention database tables, column values, or internal IDs
- Keep it to 1-3 sentences max
- You may include minor informalities: abbreviations, missing punctuation, \
  a lowercase start — but don't overdo it
- The question must be answerable through multi-step tool investigation

Respond with ONLY the question, nothing else."""

QUESTION_GEN_USER_PROMPT = "Generate a troubleshooting question for this cluster."

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
# Tool definitions
# ---------------------------------------------------------------------------

STRIP_PARAMS = {"context"}  # not supported by mock tools

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

            params = fn.get("parameters", {"type": "object", "properties": {}})
            props = {k: v for k, v in params.get("properties", {}).items() if k not in STRIP_PARAMS}
            required = [r for r in params.get("required", []) if r not in STRIP_PARAMS]

            input_schema: dict = {"type": "object", "properties": props}
            if required:
                input_schema["required"] = required

            tools.append({
                "name": name,
                "description": fn.get("description", ""),
                "input_schema": input_schema,
            })

    for extra in EXTRA_TOOL_DEFS:
        if extra["name"] not in {t["name"] for t in tools}:
            tools.append(extra)

    return tools


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

# tool_handler signature: (tool_name: str, params: dict) -> str
ToolHandler = Callable[[str, dict], str]


@dataclass
class Agent:
    system_prompt: str
    model: str
    tool_defs: list[dict]
    tool_handler: ToolHandler
    client: AnthropicVertex
    max_turns: int = 20
    thinking_budget: int = 10_000
    max_tokens: int = 16_000
    messages: list[dict] = field(default_factory=list)

    def run(self, query: str) -> str:
        """Run the agent loop for a user query. Returns the final text answer."""
        self.messages.append({"role": "user", "content": query})

        print(f"User: {query}\n")
        print("=" * 60)

        for turn in range(self.max_turns):
            print(f"\n--- Turn {turn + 1} ---")

            with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                thinking={"type": "enabled", "budget_tokens": self.thinking_budget},
                system=self.system_prompt,
                tools=self.tool_defs,
                messages=self.messages,
            ) as stream:
                response = stream.get_final_message()

            tool_uses: list = []
            text_parts: list[str] = []

            for block in response.content:
                if block.type == "tool_use":
                    tool_uses.append(block)
                elif block.type == "text":
                    text_parts.append(block.text)

            for tu in tool_uses:
                params_str = json.dumps(tu.input, separators=(",", ":")) if tu.input else "{}"
                print(f"  -> {tu.name}({params_str})")

            if not tool_uses:
                final_answer = "\n".join(text_parts)
                self.messages.append({"role": "assistant", "content": response.content})
                return final_answer

            self.messages.append({"role": "assistant", "content": response.content})

            tool_results: list[dict] = []
            for tu in tool_uses:
                result = self.tool_handler(tu.name, tu.input or {})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result,
                })

            self.messages.append({"role": "user", "content": tool_results})

        return "[Agent hit max turns without producing a final answer]"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_question(client: AnthropicVertex) -> str:
    """Use an LLM to generate a troubleshooting question from seed data."""
    seed_data = SEED_DATA_PATH.read_text()
    system = QUESTION_GEN_SYSTEM_PROMPT.format(seed_data=seed_data)

    question_agent = Agent(
        system_prompt=system,
        model="claude-opus-4-6@default",
        tool_defs=[],
        tool_handler=lambda _name, _params: "",
        client=client,
        max_turns=1,
        thinking_budget=5_000,
        max_tokens=8_000,
    )
    return question_agent.run(QUESTION_GEN_USER_PROMPT)


if __name__ == "__main__":
    client = AnthropicVertex()

    if len(sys.argv) > 1:
        query = sys.argv[1]
    else:
        print("Generating question from seed data...\n")
        query = generate_question(client)
        print(f"\nGenerated question: {query}\n")

    conn = psycopg2.connect(DB_DSN)

    def tool_handler(name: str, params: dict) -> str:
        clean = {k: v for k, v in params.items() if k not in STRIP_PARAMS}
        return call_tool(conn, name, clean)

    agent = Agent(
        system_prompt=SYSTEM_PROMPT,
        model="claude-opus-4-6@default",
        tool_defs=load_tool_defs(),
        tool_handler=tool_handler,
        client=client,
    )

    answer = agent.run(query)
    conn.close()

    print("\n" + "=" * 60)
    print("FINAL ANSWER:")
    print("=" * 60)
    print(answer)
