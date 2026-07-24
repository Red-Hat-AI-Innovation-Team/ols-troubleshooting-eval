"""Build SFT dataset from troubleshooting run traces.

Reads run JSON files from sdg/v1/*/runs/, prepends a system prompt,
reformats tool calls to OpenAI chat format, applies the model's chat
template (which injects tool definitions into the system message), and
outputs JSONL with fully-formatted text.

Usage:
    uv run python build_sft_dataset.py
    uv run python build_sft_dataset.py --output sft_dataset.jsonl --model Qwen/Qwen3.5-2B
"""

import argparse
import glob
import json
import statistics
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Build SFT dataset from run traces")
    parser.add_argument("--output", default="sft_dataset.jsonl")
    parser.add_argument("--model", default="Qwen/Qwen3.5-2B")
    parser.add_argument("--runs-dir", default="sdg/v1")
    parser.add_argument("--tools-file", default="raw_tool_defs.json")
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    tools = load_tools(args.tools_file)
    print(f"Loaded {len(tools)} tool definitions")

    run_files = sorted(glob.glob(f"{args.runs_dir}/*/runs/seed_*_run_*.json"))
    print(f"Found {len(run_files)} run files")

    written = 0
    skipped = 0
    errors = 0
    token_counts: list[int] = []

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
