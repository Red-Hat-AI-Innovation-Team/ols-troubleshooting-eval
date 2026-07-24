"""Build CPT dataset mixing domain data (90%) with FineWeb (10%).

Usage (on rh-h100-01):
  python3 build_fineweb_mix.py \
    --domain-data /home/lab/rawhad/sdg-ki-eval/combined_cut_5x.jsonl \
    --output /home/lab/rawhad/sdg-ki-eval/no_aug_cpt_data.jsonl

Reads the `document` column from domain data as the 90% slice,
streams FineWeb from HuggingFace for the 10% slice.
Output: one {"text": "..."} per line.
"""
import argparse
import json
import random


def load_domain_docs(path: str) -> list[str]:
    docs: list[str] = []
    with open(path) as f:
        for line in f:
            row = json.loads(line)
            text = row.get("text", "") or row.get("document", "")
            if text and text.strip():
                docs.append(text)
    return docs


def stream_fineweb(target_chars: int) -> list[str]:
    from datasets import load_dataset

    ds = load_dataset(
        "HuggingFaceFW/fineweb",
        name="sample-10BT",
        split="train",
        streaming=True,
    )
    docs: list[str] = []
    total_chars = 0
    for row in ds:
        text = row.get("text", "")
        if text and len(text) >= 200:
            docs.append(text)
            total_chars += len(text)
        if total_chars >= target_chars:
            break
    return docs, total_chars


def main():
    parser = argparse.ArgumentParser(description="Mix domain data (90%) + FineWeb (10%)")
    parser.add_argument("--domain-data", required=True, help="Path to combined_cut_5x.jsonl")
    parser.add_argument("--output", required=True, help="Output JSONL path")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for shuffle")
    args = parser.parse_args()

    # Load domain docs (90% of final mix by char count)
    domain_docs = load_domain_docs(args.domain_data)
    domain_chars = sum(len(d) for d in domain_docs)
    # domain_chars = 90% → fineweb_chars = domain_chars / 9
    target_fineweb_chars = domain_chars // 9
    print(f"Domain docs: {len(domain_docs)} ({domain_chars:,} chars, 90%)")
    print(f"FineWeb target: {target_fineweb_chars:,} chars (10%)")

    # Stream FineWeb until we hit the char budget
    print(f"Streaming FineWeb (sample-10BT) until {target_fineweb_chars:,} chars...")
    fineweb_docs, fineweb_chars = stream_fineweb(target_fineweb_chars)
    total_chars = domain_chars + fineweb_chars
    print(f"FineWeb docs fetched: {len(fineweb_docs)} ({fineweb_chars:,} chars)")
    print(f"Total: {len(domain_docs) + len(fineweb_docs)} docs, {total_chars:,} chars")
    print(f"Actual split: domain {domain_chars/total_chars:.1%}, fineweb {fineweb_chars/total_chars:.1%}")

    # Combine and shuffle
    all_docs = [{"text": d, "source": "domain"} for d in domain_docs] + \
               [{"text": d, "source": "fineweb"} for d in fineweb_docs]
    random.seed(args.seed)
    random.shuffle(all_docs)

    # Write output (text-only for Megatron)
    with open(args.output, "w") as f:
        for doc in all_docs:
            f.write(json.dumps({"text": doc["text"]}) + "\n")

    print(f"Written: {len(all_docs)} docs to {args.output}")
    print(f"  domain: {len(domain_docs)}, fineweb: {len(fineweb_docs)}")


if __name__ == "__main__":
    main()
