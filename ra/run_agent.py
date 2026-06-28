"""Run a troubleshooting agent loop against the mock MCP tools.

Usage:
    uv run python run_agent.py
"""

from pathlib import Path

from anthropic import AnthropicVertex

from agent import Agent
import db
import mock_tools

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

USER_SIM_SYSTEM_PROMPT = """\
You have complete knowledge of an OpenShift/Kubernetes cluster's internal state. \
Below is the full database backing this cluster — every table, every row.

CLUSTER DATABASE:
{seed_data}

You are role-playing as a mid-level SRE who just got paged or noticed something \
wrong on a dashboard. You do NOT have direct database access — you can only see \
what a real operator would see: dashboards, pager alerts, user complaints, \
kubectl output you glanced at.

You are having a conversation with an AI troubleshooting agent. Your role:

1. Your FIRST message is the initial troubleshooting question (symptoms you noticed).
2. After that, you receive the agent's investigation responses.
3. Evaluate each response against what you KNOW from the database.
4. If the agent has NOT found the true root cause yet:
   - Ask a Socratic follow-up that nudges them toward what they missed
   - Hint at symptoms or areas they haven't investigated yet
   - Do NOT give away the answer — guide them to discover it
   - Stay in character as the SRE ("hmm but what about...", "I also noticed...", \
     "did you check...")
5. If the agent HAS correctly identified the root cause and explained the \
   cascade clearly, respond with EXACTLY the word DONE on the first line, \
   followed by a brief 1-2 sentence assessment of their diagnosis.

Keep all follow-ups to 1-3 sentences. Stay casual and in character."""

INITIAL_QUESTION_PROMPT = """\
Generate a single realistic troubleshooting question. Rules:
- Sound like a real human typing in to an ai agent — casual, not a report
- Mention only 1-3 symptoms you'd actually notice first (NOT a complete inventory)
- Leave the root cause for the agent to discover
- Do NOT enumerate every affected pod/namespace/node — pick what stands out most
- Do NOT mention database tables, column values, or internal IDs
- Keep it to 1-3 sentences max
- You may include minor informalities: abbreviations, missing punctuation, \
a lowercase start — but don't overdo it
- The question must be answerable through multi-step tool investigation

Respond with ONLY the question, nothing else."""

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
# Main
# ---------------------------------------------------------------------------

MAX_CONVERSATION_ROUNDS = 5


def run(seed_data_path: Path):
    client = AnthropicVertex()
    conn = db.connect()

    # --- User simulator agent (has seed data, acts as SRE) ---
    seed_data = seed_data_path.read_text()
    user_sim = Agent(
        system_prompt=USER_SIM_SYSTEM_PROMPT.format(seed_data=seed_data),
        model="claude-opus-4-6@default",
        tool_defs=[],
        tool_handler=lambda _name, _params: "",
        client=client,
        max_turns=1,
        thinking_budget=5_000,
        max_tokens=8_000,
    )

    # --- Troubleshooting agent (has tools, no seed data) ---
    troubleshooter = Agent(
        system_prompt=SYSTEM_PROMPT,
        model="claude-opus-4-6@default",
        tool_defs=mock_tools.load_tool_defs(),
        tool_handler=mock_tools.make_tool_handler(conn),
        client=client,
    )

    # --- Generate initial question ---
    print("=" * 60)
    print("GENERATING INITIAL QUESTION")
    print("=" * 60)
    question = user_sim.run(INITIAL_QUESTION_PROMPT)
    print(f"\n>>> SRE: {question}\n")

    # --- Conversation loop ---
    for round_num in range(MAX_CONVERSATION_ROUNDS):
        print("=" * 60)
        print(f"ROUND {round_num + 1}")
        print("=" * 60)

        # Troubleshooter investigates
        answer = troubleshooter.run(question)
        print(f"\n>>> Agent: {answer[:200]}...\n")

        # User sim evaluates and responds
        follow_up = user_sim.run(answer)
        print(f"\n>>> SRE: {follow_up}\n")

        if follow_up.strip().startswith("DONE"):
            print("=" * 60)
            print("CONVERSATION COMPLETE")
            print("=" * 60)
            break

        question = follow_up

    conn.close()

if __name__ == "__main__":
  SEED_DATA_PATH = Path(__file__).parent / "seed_data.json"
  run(SEED_DATA_PATH)
