"""Pre-training gate: verify that a model checkpoint generates <tool_call> tags.

Loads a model, runs 3 sample scenario prompts through Qwen's chat template
with tool schemas, and checks that the model output contains <tool_call> tags.

Exits with code 0 on success (model generates tool calls), 1 on failure.
Run this BEFORE launching expensive GRPO training to fail fast if the SFT
warmup didn't work.

Usage:
    # Verify SFT checkpoint
    python verl_tools/verify_tool_execution.py --model checkpoints/sft-warmup-merged

    # Verify base model (expected to fail — confirms the problem)
    python verl_tools/verify_tool_execution.py --model Qwen/Qwen3-4B

    # Custom number of samples and max tokens
    python verl_tools/verify_tool_execution.py --model checkpoints/sft-warmup-merged --samples 5 --max-tokens 512
"""

import argparse
import json
import sys
from pathlib import Path

# Ensure ra/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


SAMPLE_PROMPTS = [
    "What is the issue with order-fulfillment-daemon?",
    "Why awesome-application is in CrashLoopBackOff?",
    "What is the issue with memcached?",
    "What is the issue with job inventory-sync-validator in namespace catalog-mgmt",
    "What is the status of the frontend application",
]


def load_tool_schemas() -> list[dict]:
    """Load tool schemas for the chat template."""
    raw_path = Path(__file__).resolve().parent.parent / "raw_tool_defs.json"
    with open(raw_path) as f:
        raw = json.load(f)

    tools = []
    for _server, info in raw.items():
        if not isinstance(info, dict) or "tools" not in info:
            continue
        for tool in info["tools"]:
            func = dict(tool["function"])
            func.pop("strict", None)
            props = func.get("parameters", {}).get("properties", {})
            props.pop("context", None)
            tools.append({"type": "function", "function": func})
    return tools


SYSTEM_PROMPT = """\
You are an OpenShift/Kubernetes troubleshooting agent. You have access to MCP \
tools that let you inspect a live cluster: list pods, read logs, check events, \
query Prometheus metrics, inspect alerts, exec into containers, and more.

Your job is to investigate the cluster state using these tools and provide a \
clear, evidence-based diagnosis. Do NOT guess or give generic advice — use the \
tools to gather real data and base your answer on what you find.

Be thorough but efficient. Start broad (check alerts, pod status, events) then \
drill into specific issues you discover."""


def verify(model_path: str, num_samples: int = 3, max_new_tokens: int = 256) -> bool:
    """Run sample prompts and check for <tool_call> tags in output.

    Returns True if at least one sample generates a tool call.
    """
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    print(f"Loading model: {model_path}")
    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        torch_dtype=torch.bfloat16,
        trust_remote_code=True,
        device_map="auto",
    )
    model.eval()

    tools = load_tool_schemas()
    prompts = SAMPLE_PROMPTS[:num_samples]

    success_count = 0
    total = len(prompts)

    for i, prompt in enumerate(prompts):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        text = tokenizer.apply_chat_template(
            messages,
            tools=tools,
            tokenize=False,
            add_generation_prompt=True,
        )

        inputs = tokenizer(text, return_tensors="pt").to(model.device)
        input_len = inputs["input_ids"].shape[1]

        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                temperature=None,
                top_p=None,
            )

        generated = tokenizer.decode(outputs[0][input_len:], skip_special_tokens=False)
        has_tool_call = "<tool_call>" in generated

        status = "PASS" if has_tool_call else "FAIL"
        print(f"\n  [{i+1}/{total}] {status} — {prompt[:60]}...")
        print(f"    Generated ({len(generated)} chars): {generated[:200]}...")

        if has_tool_call:
            success_count += 1

    print(f"\nResults: {success_count}/{total} samples generated <tool_call> tags")
    return success_count > 0


def main():
    parser = argparse.ArgumentParser(
        description="Verify model generates tool calls (pre-training gate)"
    )
    parser.add_argument("--model", required=True, help="Model path or HuggingFace ID")
    parser.add_argument("--samples", type=int, default=3, help="Number of sample prompts to test")
    parser.add_argument("--max-tokens", type=int, default=256, help="Max new tokens to generate")
    args = parser.parse_args()

    ok = verify(args.model, num_samples=args.samples, max_new_tokens=args.max_tokens)

    if ok:
        print("\nVERIFICATION PASSED: Model generates <tool_call> tags.")
        print("Safe to proceed with GRPO training.")
        sys.exit(0)
    else:
        print("\nVERIFICATION FAILED: Model does NOT generate <tool_call> tags.")
        print("SFT warmup is required before GRPO training.")
        print("Run: python sft/sft_warmup.py --data sft_dataset.jsonl")
        sys.exit(1)


if __name__ == "__main__":
    main()
