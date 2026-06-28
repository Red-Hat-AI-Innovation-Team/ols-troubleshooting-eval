"""Embed scenarios and find near-duplicates via cosine similarity.

Usage:
    uv run python dedup_scenarios.py              # embed + find dupes
    uv run python dedup_scenarios.py --threshold 0.80  # custom threshold
    uv run python dedup_scenarios.py --skip-embed --tsne  # visualize clusters
"""

import argparse
import json
import math
from pathlib import Path

import httpx
import numpy as np

SCENARIOS_PATH = Path(__file__).parent / "scenarios.txt"
OUTPUT_PATH = Path(__file__).parent / "scenario_embeddings.json"

EMBED_URL = "http://localhost:3000/v1/embeddings"
MODEL = "all-mpnet-base-v2"
BATCH_SIZE = 32
DEFAULT_THRESHOLD = 0.82


def load_scenarios() -> list[str]:
    lines = SCENARIOS_PATH.read_text().strip().splitlines()
    return [line.strip() for line in lines if line.strip()]


def embed_batch(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    resp = client.post(
        EMBED_URL,
        json={"input": texts, "model": MODEL},
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    data.sort(key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def find_duplicates(
    scenarios: list[str],
    embeddings: list[list[float]],
    threshold: float,
) -> list[tuple[int, int, float]]:
    """Return pairs (i, j, sim) where sim >= threshold, sorted descending."""
    n = len(scenarios)
    pairs: list[tuple[int, int, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            sim = cosine_sim(embeddings[i], embeddings[j])
            if sim >= threshold:
                pairs.append((i, j, sim))
    pairs.sort(key=lambda t: t[2], reverse=True)
    return pairs


def print_duplicates(
    scenarios: list[str],
    pairs: list[tuple[int, int, float]],
    threshold: float,
) -> None:
    if not pairs:
        print(f"\nNo pairs above threshold {threshold:.2f}")
        return

    print(f"\n{'=' * 80}")
    print(f"DUPLICATE PAIRS (cosine sim >= {threshold:.2f}): {len(pairs)} pairs")
    print(f"{'=' * 80}")

    for rank, (i, j, sim) in enumerate(pairs, 1):
        trunc_i = scenarios[i][:120] + "..." if len(scenarios[i]) > 120 else scenarios[i]
        trunc_j = scenarios[j][:120] + "..." if len(scenarios[j]) > 120 else scenarios[j]
        print(f"\n--- pair {rank} (sim={sim:.4f}) ---")
        print(f"  [{i:3d}] {trunc_i}")
        print(f"  [{j:3d}] {trunc_j}")


CATEGORY_RANGES: list[tuple[str, int, int]] = [
    ("original", 0, 12),
    ("nodes", 12, 28),
    ("pods", 28, 44),
    ("network", 44, 60),
    ("storage", 60, 74),
    ("workloads", 74, 90),
    ("resources", 90, 106),
]


def assign_category(idx: int) -> str:
    for name, start, end in CATEGORY_RANGES:
        if start <= idx < end:
            return name
    return "unknown"


def build_tsne_html(
    scenarios: list[str],
    embeddings: list[list[float]],
    pairs: list[tuple[int, int, float]],
    threshold: float,
) -> Path:
    from sklearn.manifold import TSNE
    import plotly.graph_objects as go

    X = np.array(embeddings)
    tsne = TSNE(n_components=2, perplexity=15, random_state=42, metric="cosine")
    coords = tsne.fit_transform(X)

    categories = [assign_category(i) for i in range(len(scenarios))]
    dupe_indices: set[int] = set()
    for i, j, _ in pairs:
        dupe_indices.add(i)
        dupe_indices.add(j)

    # truncate for hover
    hover_texts = [
        f"[{i}] {s[:100]}..." if len(s) > 100 else f"[{i}] {s}"
        for i, s in enumerate(scenarios)
    ]

    cat_names = list(dict.fromkeys(categories))  # preserve order
    fig = go.Figure()

    for cat in cat_names:
        idxs = [i for i, c in enumerate(categories) if c == cat]
        fig.add_trace(go.Scatter(
            x=[coords[i, 0] for i in idxs],
            y=[coords[i, 1] for i in idxs],
            mode="markers+text",
            marker=dict(
                size=[12 if i in dupe_indices else 8 for i in idxs],
                line=dict(
                    width=[2 if i in dupe_indices else 0 for i in idxs],
                    color="red",
                ),
            ),
            text=[str(i) for i in idxs],
            textposition="top center",
            textfont=dict(size=9),
            hovertext=[hover_texts[i] for i in idxs],
            hoverinfo="text",
            name=cat,
        ))

    # draw lines between dupe pairs
    for i, j, sim in pairs:
        fig.add_trace(go.Scatter(
            x=[coords[i, 0], coords[j, 0]],
            y=[coords[i, 1], coords[j, 1]],
            mode="lines",
            line=dict(color="red", width=1, dash="dot"),
            opacity=0.5,
            showlegend=False,
            hoverinfo="skip",
        ))

    fig.update_layout(
        title=f"Scenario Embeddings t-SNE (n={len(scenarios)}, dupes threshold={threshold:.2f})",
        xaxis_title="t-SNE 1",
        yaxis_title="t-SNE 2",
        width=1200,
        height=800,
        hovermode="closest",
        legend_title="Category",
    )

    out = Path(__file__).parent / "scenario_tsne.html"
    fig.write_html(str(out))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--skip-embed", action="store_true", help="Load existing embeddings")
    parser.add_argument("--tsne", action="store_true", help="Generate t-SNE plotly HTML")
    args = parser.parse_args()

    scenarios = load_scenarios()
    print(f"Loaded {len(scenarios)} scenarios")

    if args.skip_embed and OUTPUT_PATH.exists():
        print("Loading cached embeddings...")
        data = json.loads(OUTPUT_PATH.read_text())
        all_embeddings = data["embeddings"]
    else:
        all_embeddings: list[list[float]] = []
        client = httpx.Client()
        for i in range(0, len(scenarios), BATCH_SIZE):
            batch = scenarios[i : i + BATCH_SIZE]
            print(f"  Embedding batch {i // BATCH_SIZE + 1} ({len(batch)} texts)...")
            embeddings = embed_batch(client, batch)
            all_embeddings.extend(embeddings)
        client.close()

        output = {
            "model": MODEL,
            "count": len(scenarios),
            "dim": len(all_embeddings[0]),
            "scenarios": scenarios,
            "embeddings": all_embeddings,
        }
        OUTPUT_PATH.write_text(json.dumps(output))
        print(f"Wrote {len(scenarios)} embeddings (dim={len(all_embeddings[0])}) to {OUTPUT_PATH.name}")

    # --- pairwise cosine similarity ---
    print(f"\nComputing pairwise cosine similarity ({len(scenarios)} x {len(scenarios)})...")
    pairs = find_duplicates(scenarios, all_embeddings, args.threshold)
    print_duplicates(scenarios, pairs, args.threshold)

    # --- summary: which indices appear in dupe pairs ---
    dupe_indices: set[int] = set()
    for i, j, _ in pairs:
        dupe_indices.add(i)
        dupe_indices.add(j)
    print(f"\n{len(dupe_indices)} scenarios involved in at least one dupe pair")
    print(f"{len(scenarios) - len(dupe_indices)} scenarios are unique (no pair above threshold)")

    if args.tsne:
        out = build_tsne_html(scenarios, all_embeddings, pairs, args.threshold)
        print(f"\nWrote t-SNE visualization to {out}")


if __name__ == "__main__":
    main()
