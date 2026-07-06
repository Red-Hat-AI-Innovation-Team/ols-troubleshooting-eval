"""Build CPT dataset in JSONL format from doc repos + SO CSVs.

Two sources:
1. Doc repos: repos/**/*.{md,txt,adoc,rst} -> one {"text": ...} per file
2. SO CSVs: so_data/*.csv -> global dedup by QuestionId -> html_to_text -> one {"text": ...} per Q+A

Output: cpt_dataset.jsonl (one JSON object per line)
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

from html_to_text import html_to_text

csv.field_size_limit(sys.maxsize)

# --- Config ---
REPOS_DIR = Path("repos")
SO_DIR = Path("so_data")
OUT_PATH = Path("cpt_dataset.jsonl")

DOC_EXTENSIONS = {".md", ".txt", ".adoc", ".rst"}
MIN_DOC_CHARS = 50  # skip tiny files (empty READMEs, index stubs)
MIN_SO_CHARS = 100  # skip trivially short SO posts


def iter_doc_files() -> list[Path]:
    """Find all doc files across repos."""
    files = []
    for ext in DOC_EXTENSIONS:
        files.extend(REPOS_DIR.rglob("*" + ext))
    return sorted(files)


def process_docs(out_f) -> dict:
    """Write doc repo files as JSONL. Returns stats."""
    files = iter_doc_files()
    written = 0
    skipped = 0

    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(text) < MIN_DOC_CHARS:
            skipped += 1
            continue

        # source tag for provenance
        repo = path.relative_to(REPOS_DIR).parts[0] if REPOS_DIR.exists() else "unknown"
        rel = str(path.relative_to(REPOS_DIR))

        doc = {"text": text, "source": "docs", "repo": repo, "path": rel}
        out_f.write(json.dumps(doc, ensure_ascii=False) + "\n")
        written += 1

    return {"written": written, "skipped": skipped, "total_files": len(files)}


def process_so(out_f) -> dict:
    """Process all SO CSVs with global dedup by QuestionId. Returns stats."""
    csvs = sorted(SO_DIR.glob("single__*.csv")) + sorted(SO_DIR.glob("cross__*.csv"))

    seen_ids: set[str] = set()
    written = 0
    dupes = 0
    skipped_short = 0

    for csv_path in csvs:
        if csv_path.stat().st_size < 200:
            continue

        tag = csv_path.stem.replace("single__", "").replace("cross__", "x_")

        with csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                qid = row.get("QuestionId", "")
                if qid in seen_ids:
                    dupes += 1
                    continue
                seen_ids.add(qid)

                title = row.get("Title", "").strip()
                q_html = row.get("QuestionBody", "")
                a_html = row.get("AnswerBody", "")

                q_text = html_to_text(q_html)
                a_text = html_to_text(a_html)

                if not q_text:
                    skipped_short += 1
                    continue

                # Build document
                text = "## " + title + "\n\n" + q_text
                if a_text:
                    text += "\n\n### Answer\n\n" + a_text

                if len(text) < MIN_SO_CHARS:
                    skipped_short += 1
                    continue

                doc = {"text": text, "source": "stackoverflow", "tag": tag, "qid": qid}
                out_f.write(json.dumps(doc, ensure_ascii=False) + "\n")
                written += 1

    return {"written": written, "dupes_removed": dupes, "skipped_short": skipped_short}


def main():
    t0 = time.time()

    with OUT_PATH.open("w", encoding="utf-8") as out_f:
        # 1. Docs
        print("Processing doc repos...")
        t1 = time.time()
        doc_stats = process_docs(out_f)
        print("  files: {}, written: {}, skipped: {} ({:.1f}s)".format(
            doc_stats["total_files"], doc_stats["written"],
            doc_stats["skipped"], time.time() - t1
        ))

        # 2. SO
        print("Processing SO CSVs (global dedup)...")
        t2 = time.time()
        so_stats = process_so(out_f)
        print("  written: {}, dupes_removed: {}, skipped_short: {} ({:.1f}s)".format(
            so_stats["written"], so_stats["dupes_removed"],
            so_stats["skipped_short"], time.time() - t2
        ))

    # Summary
    size_mb = OUT_PATH.stat().st_size / (1024 * 1024)
    total_docs = doc_stats["written"] + so_stats["written"]
    elapsed = time.time() - t0

    print()
    print("=" * 60)
    print("Dataset: {}".format(OUT_PATH))
    print("  Total documents: {:,}".format(total_docs))
    print("    Docs:          {:,}".format(doc_stats["written"]))
    print("    SO:            {:,}".format(so_stats["written"]))
    print("  SO dupes removed: {:,}".format(so_stats["dupes_removed"]))
    print("  File size:       {:.1f} MB".format(size_mb))
    print("  Time:            {:.1f}s".format(elapsed))
    print("=" * 60)


if __name__ == "__main__":
    main()
