"""Build training dataset mixing CPT data (80%) with instruction-following data (20%).

Usage (on rh-h100-01):
  python3 build_if_mix.py \
    --cpt-data /home/lab/rawhad/sdg-ki-eval/no_aug_cpt_data.jsonl \
    --model-id nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-Base-BF16 \
    --output /home/lab/rawhad/sdg-ki-eval/train_data.jsonl

IF data is allenai/tulu-3-sft-mixture with chat template applied via
the model's tokenizer. Model ID is appended to the output filename:
  train_data__nvidia__NVIDIA-Nemotron-3-Nano-30B-A3B-Base-BF16.jsonl

Split is 80% CPT / 20% IF by character count.
Output: one {"text": "..."} per line.
"""
import argparse
import json
import random
from pathlib import Path


def load_cpt_docs(path: str) -> list[str]:
    docs: list[str] = []
    with open(path) as f:
        for line in f:
            row = json.loads(line)
            text = row.get("text", "")
            if text and text.strip():
                docs.append(text)
    return docs


def stream_tulu3_with_template(target_chars: int, model_id: str) -> tuple[list[str], int]:
    from datasets import load_dataset
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)

    ds = load_dataset(
        "allenai/tulu-3-sft-mixture",
        split="train",
        streaming=True,
    )

    docs: list[str] = []
    total_chars = 0
    for row in ds:
        messages = row.get("messages", [])
        if not messages:
            continue
        text = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False,
        )
        if text and len(text) >= 100:
            docs.append(text)
            total_chars += len(text)
        if total_chars >= target_chars:
            break

    return docs, total_chars


def make_output_path(output: str, model_id: str) -> str:
    """Append model id (with / replaced by __) to the output filename."""
    p = Path(output)
    model_slug = model_id.replace("/", "__")
    return str(p.with_stem(f"{p.stem}__{model_slug}"))


def main():
    parser = argparse.ArgumentParser(description="Mix CPT (80%) + IF (20%) by char count")
    parser.add_argument("--cpt-data", required=True, help="Path to CPT JSONL (e.g. no_aug_cpt_data.jsonl)")
    parser.add_argument("--model-id", required=True, help="HF model ID for chat template")
    parser.add_argument("--output", required=True, help="Output JSONL path (model ID appended automatically)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for shuffle")
    args = parser.parse_args()

    output_path = make_output_path(args.output, args.model_id)

    # Load CPT docs (80% of final mix by char count)
    cpt_docs = load_cpt_docs(args.cpt_data)
    cpt_chars = sum(len(d) for d in cpt_docs)
    # cpt_chars = 80% → if_chars = cpt_chars / 4
    target_if_chars = cpt_chars // 4
    print(f"CPT docs: {len(cpt_docs)} ({cpt_chars:,} chars, 80%)")
    print(f"IF target: {target_if_chars:,} chars (20%)")
    print(f"Model: {args.model_id}")

    # Stream tulu-3 with chat template
    print(f"Streaming tulu-3-sft-mixture with chat template...")
    if_docs, if_chars = stream_tulu3_with_template(target_if_chars, args.model_id)
    total_chars = cpt_chars + if_chars
    print(f"IF docs fetched: {len(if_docs)} ({if_chars:,} chars)")
    print(f"Total: {len(cpt_docs) + len(if_docs)} docs, {total_chars:,} chars")
    print(f"Actual split: CPT {cpt_chars/total_chars:.1%}, IF {if_chars/total_chars:.1%}")

    # Combine and shuffle
    all_docs = [{"text": d} for d in cpt_docs] + \
               [{"text": d} for d in if_docs]
    random.seed(args.seed)
    random.shuffle(all_docs)

    # Write output
    with open(output_path, "w") as f:
        for doc in all_docs:
            f.write(json.dumps(doc) + "\n")

    print(f"Written: {len(all_docs)} docs to {output_path}")
    print(f"  CPT: {len(cpt_docs)}, IF: {len(if_docs)}")


if __name__ == "__main__":
    main()
