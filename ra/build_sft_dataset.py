"""Build SFT dataset from troubleshooting run traces.

Two modes:
  1. --from-runs (default): Read pre-existing run JSON files from sdg/v1/*/runs/
  2. --from-eval: Generate fresh traces by running the 11 eval scenarios through
     the agent loop against a strong model, then filter by judge score >= threshold.

Both modes reformat tool calls to OpenAI chat format, apply the model's chat
template (which injects tool definitions via Hermes-style <tool_call> tags),
and output JSONL with fully-formatted text.

Usage:
    # From existing run files
    uv run python build_sft_dataset.py
    uv run python build_sft_dataset.py --output sft_dataset.jsonl --model Qwen/Qwen3-4B

    # From eval scenarios (generates fresh traces via agent loop)
    uv run python build_sft_dataset.py --from-eval --min-score 0.7 --iterations 3
    uv run python build_sft_dataset.py --from-eval --troubleshooter-model gpt-5-mini
"""

import argparse
import glob
import json
import statistics
import sys
from pathlib import Path

from transformers import AutoTokenizer


SYSTEM_PROMPT = """\
You are an OpenShift/Kubernetes troubleshooting agent. You have access to MCP \
tools that let you inspect a live cluster: list pods, read logs, check events, \
query Prometheus metrics, inspect alerts, exec into containers, and more.

Your job is to investigate the cluster state using these tools and provide a \
clear, evidence-based diagnosis. Do NOT guess or give generic advice — use the \
tools to gather real data and base your answer on what you find.

Be thorough but efficient. Start broad (check alerts, pod status, events) then \
drill into specific issues you discover."""


def load_tools(path: str | Path) -> list[dict]:
    """Load tool definitions from raw_tool_defs.json, clean up for chat template."""
    with open(path) as f:
        raw = json.load(f)
    tools = []
    for _server, info in raw.items():
        if not isinstance(info, dict) or "tools" not in info:
            continue
        for tool in info["tools"]:
            func = dict(tool["function"])
            func.pop("strict", None)
            # Remove cluster-specific 'context' parameter
            props = func.get("parameters", {}).get("properties", {})
            props.pop("context", None)
            tools.append({"type": "function", "function": func})
    return tools


def convert_conversation(conv: list[dict]) -> list[dict]:
    """Convert our run format to OpenAI chat messages for apply_chat_template."""
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Build tool_call_id -> tool_name map for tool result messages
    tc_id_to_name: dict[str, str] = {}
    for msg in conv:
        for tc in msg.get("tool_calls", []):
            tc_id_to_name[tc["id"]] = tc["name"]

    for msg in conv:
        role = msg["role"]

        if role == "user":
            messages.append({"role": "user", "content": msg["content"]})

        elif role == "assistant":
            m: dict = {"role": "assistant"}
            if msg.get("content"):
                m["content"] = msg["content"]
            if msg.get("tool_calls"):
                m["tool_calls"] = [
                    {
                        "type": "function",
                        "id": tc["id"],
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],  # dict, not JSON string
                        },
                    }
                    for tc in msg["tool_calls"]
                ]
            messages.append(m)

        elif role == "tool":
            for tr in msg.get("tool_results", []):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_call_id"],
                    "name": tc_id_to_name.get(tr["tool_call_id"], "unknown"),
                    "content": tr["content"],
                })

    return messages


def convert_message_objects(messages: list) -> list[dict]:
    """Convert Message/pydantic objects (from agent loop) to plain dicts for apply_chat_template.

    The agent loop returns Message objects with tool_calls as ToolCall objects.
    This converts them to the OpenAI chat format expected by apply_chat_template.
    """
    out: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Build tool_call_id -> tool_name map
    tc_id_to_name: dict[str, str] = {}
    for msg in messages:
        msg_dict = msg if isinstance(msg, dict) else msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
        for tc in msg_dict.get("tool_calls", []):
            tc_id_to_name[tc["id"]] = tc["name"]

    for msg in messages:
        msg_dict = msg if isinstance(msg, dict) else msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
        role = msg_dict["role"]

        if role == "user":
            out.append({"role": "user", "content": msg_dict["content"]})

        elif role == "assistant":
            m: dict = {"role": "assistant"}
            if msg_dict.get("content"):
                m["content"] = msg_dict["content"]
            if msg_dict.get("tool_calls"):
                m["tool_calls"] = [
                    {
                        "type": "function",
                        "id": tc["id"],
                        "function": {
                            "name": tc["name"],
                            "arguments": tc.get("arguments", {}),
                        },
                    }
                    for tc in msg_dict["tool_calls"]
                ]
            out.append(m)

        elif role == "tool":
            for tr in msg_dict.get("tool_results", []):
                out.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_call_id"],
                    "name": tc_id_to_name.get(tr["tool_call_id"], "unknown"),
                    "content": tr["content"],
                })

    return out


def generate_eval_traces(
    troubleshooter_model: str,
    judge_model: str,
    model_url: str | None,
    min_score: float,
    iterations: int,
) -> list[dict]:
    """Generate SFT traces by running all 11 eval scenarios through the agent loop.

    Runs each scenario `iterations` times against `troubleshooter_model`, scores
    with the judge, and returns traces that score >= min_score.

    Returns list of dicts with keys: messages (list[dict]), scenario_id, score, query, expected_response.
    """
    from eval.harness import run_scenario
    from eval.scenarios import list_scenarios
    from llm import OpenAIClient
    from llm.config import OpenAIConfig

    # Build clients
    if model_url:
        troubleshooter_client = OpenAIClient(OpenAIConfig(base_url=model_url))
    else:
        # Default: use OpenAI API directly
        troubleshooter_client = OpenAIClient(OpenAIConfig())

    judge_client = OpenAIClient(OpenAIConfig())

    scenario_ids = list_scenarios()
    traces: list[dict] = []

    print(f"Generating traces: {len(scenario_ids)} scenarios × {iterations} iterations")
    print(f"Troubleshooter: {troubleshooter_model}, Judge: {judge_model}, Min score: {min_score}")

    for scenario_id in scenario_ids:
        for iteration in range(iterations):
            print(f"\n  [{scenario_id}] iteration {iteration + 1}/{iterations}...", end=" ")
            try:
                result = run_scenario(
                    scenario_id=scenario_id,
                    troubleshooter_client=troubleshooter_client,
                    troubleshooter_model=troubleshooter_model,
                    judge_client=judge_client,
                    judge_model=judge_model,
                )

                score = result.mean_score
                status = "PASS" if score >= min_score else "skip"
                print(f"score={score:.2f} [{status}]")

                if score >= min_score:
                    # Use the last turn's conversation (contains full tool-calling trace)
                    last_turn = result.turns[-1]
                    first_turn = result.turns[0]
                    traces.append({
                        "messages": last_turn.conversation,
                        "scenario_id": scenario_id,
                        "score": score,
                        "query": first_turn.query,
                        "expected_response": first_turn.expected_response,
                        "iteration": iteration,
                    })

            except Exception as e:
                print(f"ERROR: {e}")

    print(f"\nGenerated {len(traces)} passing traces from {len(scenario_ids) * iterations} total runs")
    return traces


def main() -> None:
    parser = argparse.ArgumentParser(description="Build SFT dataset from run traces")
    parser.add_argument("--output", default="sft_dataset.jsonl")
    parser.add_argument("--model", default="Qwen/Qwen3-4B",
                        help="Tokenizer model for chat template formatting")
    parser.add_argument("--tools-file", default="raw_tool_defs.json")

    # Mode: from existing runs (default) or from eval scenarios
    parser.add_argument("--from-eval", action="store_true",
                        help="Generate traces by running eval scenarios through the agent loop")
    parser.add_argument("--runs-dir", default="sdg/v1",
                        help="Directory with pre-existing run files (used when --from-eval is not set)")

    # --from-eval options
    parser.add_argument("--troubleshooter-model", default="gpt-5-mini",
                        help="Strong model to generate traces (default: gpt-5-mini)")
    parser.add_argument("--judge-model", default="gpt-5-mini",
                        help="Judge model for scoring traces (default: gpt-5-mini)")
    parser.add_argument("--model-url", default=None,
                        help="OpenAI-compatible endpoint for the troubleshooter model")
    parser.add_argument("--min-score", type=float, default=0.7,
                        help="Minimum judge score to include a trace (default: 0.7)")
    parser.add_argument("--iterations", type=int, default=3,
                        help="Number of times to run each scenario (default: 3)")

    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    tools = load_tools(args.tools_file)
    print(f"Loaded {len(tools)} tool definitions")

    written = 0
    skipped = 0
    errors = 0
    token_counts: list[int] = []

    if args.from_eval:
        # Mode 2: Generate fresh traces from eval scenarios
        traces = generate_eval_traces(
            troubleshooter_model=args.troubleshooter_model,
            judge_model=args.judge_model,
            model_url=args.model_url,
            min_score=args.min_score,
            iterations=args.iterations,
        )

        with open(args.output, "w") as out:
            for trace in traces:
                messages = convert_message_objects(trace["messages"])

                try:
                    text = tokenizer.apply_chat_template(
                        messages,
                        tools=tools,
                        tokenize=False,
                        add_generation_prompt=False,
                    ).rstrip('\n')
                except Exception as e:
                    print(f"  template error for {trace['scenario_id']}: {e}")
                    errors += 1
                    continue

                n_tokens = len(tokenizer.encode(text))
                token_counts.append(n_tokens)

                record = {
                    "text": text,
                    "scenario_id": trace["scenario_id"],
                    "score": trace["score"],
                    "query": trace["query"],
                    "expected_response": trace["expected_response"],
                    "iteration": trace["iteration"],
                }
                out.write(json.dumps(record) + "\n")
                written += 1

    else:
        # Mode 1: Read from existing run files
        run_files = sorted(glob.glob(f"{args.runs_dir}/*/runs/seed_*_run_*.json"))
        print(f"Found {len(run_files)} run files")

        with open(args.output, "w") as out:
            for rf in run_files:
                with open(rf) as f:
                    data = json.load(f)

                conv = data["conversation"]
                if len(conv) < 2:
                    skipped += 1
                    continue

                messages = convert_conversation(conv)

                text = tokenizer.apply_chat_template(
                    messages,
                    tools=tools,
                    tokenize=False,
                    add_generation_prompt=False,
                ).rstrip('\n')

                n_tokens = len(tokenizer.encode(text))
                token_counts.append(n_tokens)

                record = {
                    "text": text,
                    "scenario_idx": data["scenario_idx"],
                    "seed_idx": data["seed_idx"],
                    "run_idx": data["run_idx"],
                }
                out.write(json.dumps(record) + "\n")
                written += 1

    print(f"\nWritten: {written}, Skipped: {skipped}, Errors: {errors}")
    if token_counts:
        sorted_counts = sorted(token_counts)
        p95_idx = int(len(sorted_counts) * 0.95)
        print(
            f"Tokens: min={min(token_counts)}, "
            f"median={int(statistics.median(token_counts))}, "
            f"mean={int(statistics.mean(token_counts))}, "
            f"P95={sorted_counts[p95_idx]}, "
            f"max={max(token_counts)}, "
            f"total={sum(token_counts):,}"
        )
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
