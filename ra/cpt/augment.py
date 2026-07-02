"""Generate FinePhrase-style augmentations for CPT data.

Reads cpt_dataset.jsonl, applies 4 augmentation prompts (FAQ, Math, Table, Tutorial)
via an OpenAI-compatible API, writes one output JSONL per augmentation type.

Usage:
  # Test on 10 docs
  uv run python augment.py --limit 10

  # Full run (all docs, all augmentations)
  uv run python augment.py

  # Single augmentation type
  uv run python augment.py --types faq tutorial

  # Custom endpoint (e.g., local vLLM)
  uv run python augment.py --base-url http://localhost:8000/v1 --model Qwen/Qwen3.5-2B
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

# --- Prompts (verbatim from FinePhrase appendix) ---

PROMPTS: dict[str, str] = {
    "faq": (
        "Rewrite the document as a comprehensive FAQ (Frequently Asked Questions). "
        "Extract or infer the key questions a reader would have about this topic, "
        "then provide clear, direct answers. Order questions logically—from foundational "
        "to advanced, or by topic area. Each answer should be self-contained and "
        "understandable without reference to other answers. Ensure the FAQ works as a "
        "standalone document. Output only the FAQ, nothing else.\n"
        "Document:\n"
    ),
    "math": (
        "Rewrite the document to create a mathematical word problem based on the "
        "numerical data or relationships in the text. Provide a step-by-step solution "
        "that shows the calculation process clearly. Create a problem that requires "
        "multi-step reasoning and basic arithmetic operations. It should include the "
        "question followed by a detailed solution showing each calculation step. "
        "Output only the problem and solution, nothing else.\n"
        "Document:\n"
    ),
    "table": (
        "Rewrite the document as a structured table that organizes the key information, "
        "then generate one question-answer pair based on the table. First extract the "
        "main data points and organize them into a clear table format with appropriate "
        "headers using markdown table syntax with proper alignment. After the table, "
        "generate one insightful question that can be answered using the table data. "
        "Provide a clear, concise answer to the question based on the information in "
        "the table. Output only the table followed by the question-answer pair, nothing else.\n"
        "Document:\n"
    ),
    "tutorial": (
        "Rewrite the document as a clear, step-by-step tutorial or instructional guide. "
        "Use numbered steps or bullet points where appropriate to enhance clarity. "
        "Preserve all essential information while ensuring the style feels didactic "
        "and easy to follow. Output only the tutorial, nothing else.\n"
        "Document:\n"
    ),
}

ALL_TYPES = list(PROMPTS.keys())

MIN_CHARS = 100  # skip trivially short docs


@dataclass
class Stats:
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    total_output_chars: int = 0
    start_time: float = 0.0

    def elapsed(self) -> float:
        return time.time() - self.start_time

    def summary(self) -> str:
        e = self.elapsed()
        return (
            f"completed={self.completed} skipped={self.skipped} failed={self.failed} "
            f"output_chars={self.total_output_chars:,} elapsed={e:.1f}s"
        )


async def augment_doc(
    client: AsyncOpenAI,
    model: str,
    prompt_prefix: str,
    doc: dict,
    semaphore: asyncio.Semaphore,
    max_tokens: int,
) -> dict | None:
    """Send one doc through the API. Returns augmented doc dict or None on failure."""
    text = doc.get("text", "")
    if len(text) < MIN_CHARS:
        return None

    user_content = prompt_prefix + text

    async with semaphore:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": user_content}],
            max_tokens=max_tokens,
            temperature=0.7,
        )

    output = resp.choices[0].message.content or ""
    if not output.strip():
        return None

    result = {
        "text": output.strip(),
        "augmentation": "",  # filled by caller
        "original_chars": len(text),
    }
    # carry over source metadata
    for key in ("source", "repo", "path", "tag", "qid"):
        if key in doc:
            result[key] = doc[key]

    return result


async def run_augmentation(
    client: AsyncOpenAI,
    model: str,
    aug_type: str,
    docs: list[dict],
    output_path: Path,
    concurrency: int,
    max_tokens: int,
) -> Stats:
    """Run one augmentation type across all docs."""
    prompt_prefix = PROMPTS[aug_type]
    semaphore = asyncio.Semaphore(concurrency)
    stats = Stats(start_time=time.time())

    # create all tasks
    tasks: list[asyncio.Task[dict | None]] = []
    skip_indices: set[int] = set()
    for i, doc in enumerate(docs):
        if len(doc.get("text", "")) < MIN_CHARS:
            skip_indices.add(i)
            stats.skipped += 1
            continue
        task = asyncio.create_task(
            augment_doc(client, model, prompt_prefix, doc, semaphore, max_tokens)
        )
        tasks.append(task)

    # collect results and write as they complete
    with output_path.open("w", encoding="utf-8") as f:
        for coro in asyncio.as_completed(tasks):
            result = await coro
            if result is None:
                stats.failed += 1
                continue
            result["augmentation"] = aug_type
            stats.completed += 1
            stats.total_output_chars += len(result["text"])
            f.write(json.dumps(result, ensure_ascii=False) + "\n")

            # progress every 50 docs
            if stats.completed % 50 == 0:
                print(f"  [{aug_type}] {stats.summary()}")

    return stats


def load_docs(input_path: Path, limit: int | None) -> list[dict]:
    """Load JSONL docs, optionally limited."""
    docs: list[dict] = []
    with input_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            docs.append(json.loads(line))
            if limit and len(docs) >= limit:
                break
    return docs


async def main_async(args: argparse.Namespace) -> None:
    client = AsyncOpenAI(
        api_key=os.environ.get("DEEPSEEK_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
        base_url=args.base_url,
    )

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading docs from {input_path} (limit={args.limit})...")
    docs = load_docs(input_path, args.limit)
    print(f"Loaded {len(docs)} docs")

    aug_types = args.types or ALL_TYPES
    print(f"Augmentation types: {aug_types}")
    print(f"Model: {args.model}")
    print(f"Concurrency: {args.concurrency}")
    print()

    for aug_type in aug_types:
        output_path = output_dir / f"cpt_aug_{aug_type}.jsonl"
        print(f"[{aug_type}] Starting -> {output_path}")
        stats = await run_augmentation(
            client=client,
            model=args.model,
            aug_type=aug_type,
            docs=docs,
            output_path=output_path,
            concurrency=args.concurrency,
            max_tokens=args.max_tokens,
        )
        print(f"[{aug_type}] Done: {stats.summary()}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="FinePhrase-style CPT augmentation")
    parser.add_argument("--input", default="cpt_dataset.jsonl", help="Input JSONL path")
    parser.add_argument("--output-dir", default="augmented", help="Output directory")
    parser.add_argument("--limit", type=int, default=None, help="Max docs to process (for testing)")
    parser.add_argument("--types", nargs="+", choices=ALL_TYPES, default=None,
                        help="Augmentation types to run (default: all)")
    parser.add_argument("--model", default="deepseek-chat", help="Model name")
    parser.add_argument("--base-url", default="https://api.deepseek.com/v1",
                        help="OpenAI-compatible API base URL")
    parser.add_argument("--concurrency", type=int, default=64, help="Max concurrent requests")
    parser.add_argument("--max-tokens", type=int, default=4096, help="Max output tokens per doc")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
