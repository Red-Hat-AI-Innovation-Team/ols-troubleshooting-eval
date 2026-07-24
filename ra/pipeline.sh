#!/usr/bin/env bash
# pipeline.sh — End-to-end OLS training pipeline
#
# Chains: seed generation → single-turn agent runs → SFT dataset build → (optional) training
#
# Usage:
#   ./pipeline.sh                          # run full pipeline (no training)
#   ./pipeline.sh --train                  # include training step
#   ./pipeline.sh --dry-run                # print commands without executing
#   ./pipeline.sh --seeds 10 --runs 5      # override counts
#
# Environment variables:
#   SEEDS          — seeds per scenario (default: 5)
#   RUNS           — runs per seed (default: 5)
#   OUTPUT_DIR     — output directory (default: sdg/v1)
#   MODEL          — training base model (default: Qwen/Qwen3-8B)
#   MODEL_URL      — OpenAI-compatible endpoint for troubleshooter (optional)
#   MODEL_NAME     — troubleshooter model name (default: claude-haiku-4-5@20251001)
#   MODE           — conversation mode: single-turn or multi-turn (default: single-turn)
#   SFT_OUTPUT     — SFT dataset output path (default: sft_dataset.jsonl)
#   TRAIN_OUTPUT   — training output directory (default: ols-agent-sft)
#   EPOCHS         — training epochs (default: 3)
#   BATCH_SIZE     — per-device batch size (default: 1)
#   LR             — learning rate (default: 1e-4)
#   MAX_SEQ_LENGTH — max sequence length (default: 8192)
#   CONCURRENCY    — parallel agent runs (default: 50)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# -----------------------------------------------------------------------
# Defaults (overridable via env vars or CLI args)
# -----------------------------------------------------------------------
SEEDS="${SEEDS:-5}"
RUNS="${RUNS:-5}"
OUTPUT_DIR="${OUTPUT_DIR:-sdg/v1}"
MODEL="${MODEL:-Qwen/Qwen3-8B}"
MODEL_URL="${MODEL_URL:-}"
MODEL_NAME="${MODEL_NAME:-claude-haiku-4-5@20251001}"
MODE="${MODE:-single-turn}"
SFT_OUTPUT="${SFT_OUTPUT:-sft_dataset.jsonl}"
TRAIN_OUTPUT="${TRAIN_OUTPUT:-ols-agent-sft}"
EPOCHS="${EPOCHS:-3}"
BATCH_SIZE="${BATCH_SIZE:-1}"
LR="${LR:-1e-4}"
MAX_SEQ_LENGTH="${MAX_SEQ_LENGTH:-8192}"
CONCURRENCY="${CONCURRENCY:-50}"

DRY_RUN=false
DO_TRAIN=false

# -----------------------------------------------------------------------
# Parse CLI args (override env vars)
# -----------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --seeds)       SEEDS="$2";       shift 2 ;;
        --runs)        RUNS="$2";        shift 2 ;;
        --output-dir)  OUTPUT_DIR="$2";  shift 2 ;;
        --model)       MODEL="$2";       shift 2 ;;
        --model-url)   MODEL_URL="$2";   shift 2 ;;
        --model-name)  MODEL_NAME="$2";  shift 2 ;;
        --mode)        MODE="$2";        shift 2 ;;
        --sft-output)  SFT_OUTPUT="$2";  shift 2 ;;
        --epochs)      EPOCHS="$2";      shift 2 ;;
        --batch-size)  BATCH_SIZE="$2";  shift 2 ;;
        --lr)          LR="$2";          shift 2 ;;
        --max-seq-length) MAX_SEQ_LENGTH="$2"; shift 2 ;;
        --concurrency) CONCURRENCY="$2"; shift 2 ;;
        --train)       DO_TRAIN=true;    shift ;;
        --dry-run)     DRY_RUN=true;     shift ;;
        --help|-h)
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --seeds N          Seeds per scenario (default: $SEEDS)"
            echo "  --runs N           Runs per seed (default: $RUNS)"
            echo "  --output-dir DIR   Output directory (default: $OUTPUT_DIR)"
            echo "  --model NAME       Training base model (default: $MODEL)"
            echo "  --model-url URL    OpenAI-compatible endpoint for troubleshooter"
            echo "  --model-name NAME  Troubleshooter model name (default: $MODEL_NAME)"
            echo "  --mode MODE        single-turn or multi-turn (default: $MODE)"
            echo "  --sft-output FILE  SFT dataset output (default: $SFT_OUTPUT)"
            echo "  --epochs N         Training epochs (default: $EPOCHS)"
            echo "  --batch-size N     Per-device batch size (default: $BATCH_SIZE)"
            echo "  --lr RATE          Learning rate (default: $LR)"
            echo "  --max-seq-length N Max sequence length (default: $MAX_SEQ_LENGTH)"
            echo "  --concurrency N    Parallel agent runs (default: $CONCURRENCY)"
            echo "  --train            Include training step"
            echo "  --dry-run          Print commands without executing"
            echo "  -h, --help         Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------
run_cmd() {
    echo ""
    echo ">>> $*"
    if [ "$DRY_RUN" = true ]; then
        echo "    [dry-run, skipped]"
    else
        "$@"
    fi
}

step() {
    echo ""
    echo "========================================================================"
    echo "  $1"
    echo "========================================================================"
}

# -----------------------------------------------------------------------
# Prerequisites check
# -----------------------------------------------------------------------
step "Checking prerequisites"

check_cmd() {
    if command -v "$1" &>/dev/null; then
        echo "  ✓ $1"
    else
        echo "  ✗ $1 not found" >&2
        return 1
    fi
}

check_cmd uv || { echo "ERROR: uv is required. Install from https://docs.astral.sh/uv/"; exit 1; }
check_cmd python3 || check_cmd python || { echo "ERROR: python not found"; exit 1; }

# Check PostgreSQL (needed for mock tools)
if command -v pg_isready &>/dev/null; then
    if pg_isready -q 2>/dev/null; then
        echo "  ✓ PostgreSQL is running"
    else
        echo "  ⚠ PostgreSQL not responding (pg_isready failed)"
        echo "    Pipeline may fail during agent runs"
    fi
else
    echo "  ⚠ pg_isready not found, cannot verify PostgreSQL status"
fi

# Check Python deps
echo ""
echo "Checking Python dependencies..."
if [ "$DRY_RUN" = false ]; then
    uv run python -c "import psycopg2; import transformers" 2>/dev/null \
        && echo "  ✓ Core Python dependencies available" \
        || echo "  ⚠ Some Python dependencies may be missing (will install on first uv run)"
fi

# -----------------------------------------------------------------------
# Build model-url args
# -----------------------------------------------------------------------
MODEL_URL_ARG=""
if [ -n "$MODEL_URL" ]; then
    MODEL_URL_ARG="--model-url $MODEL_URL"
fi

# -----------------------------------------------------------------------
# Stage 1: Seed generation
# -----------------------------------------------------------------------
step "Stage 1: Seed generation ($SEEDS seeds per scenario)"

run_cmd uv run python main.py \
    --stage seed \
    --seeds "$SEEDS" \
    --output-dir "$OUTPUT_DIR"

# -----------------------------------------------------------------------
# Stage 2: Agent runs (single-turn mode by default)
# -----------------------------------------------------------------------
step "Stage 2: Agent runs ($RUNS runs per seed, mode=$MODE)"

run_cmd uv run python main.py \
    --stage run \
    --seeds "$SEEDS" \
    --runs "$RUNS" \
    --output-dir "$OUTPUT_DIR" \
    --mode "$MODE" \
    --model-name "$MODEL_NAME" \
    --concurrency "$CONCURRENCY" \
    $MODEL_URL_ARG

# -----------------------------------------------------------------------
# Stage 3: Build SFT dataset
# -----------------------------------------------------------------------
step "Stage 3: Build SFT dataset"

run_cmd uv run python build_sft_dataset.py \
    --output "$SFT_OUTPUT" \
    --model "$MODEL" \
    --runs-dir "$OUTPUT_DIR"

# -----------------------------------------------------------------------
# Stage 4: Training (optional)
# -----------------------------------------------------------------------
if [ "$DO_TRAIN" = true ]; then
    step "Stage 4: Training ($EPOCHS epochs, model=$MODEL)"

    run_cmd uv run python train_ols_agent.py \
        --dataset "$SFT_OUTPUT" \
        --model "$MODEL" \
        --output-dir "$TRAIN_OUTPUT" \
        --epochs "$EPOCHS" \
        --batch-size "$BATCH_SIZE" \
        --lr "$LR" \
        --max-seq-length "$MAX_SEQ_LENGTH"
else
    step "Stage 4: Training (skipped — use --train to enable)"
fi

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
step "Pipeline complete"

echo "  Output directory:  $OUTPUT_DIR"
echo "  SFT dataset:       $SFT_OUTPUT"
if [ "$DO_TRAIN" = true ]; then
    echo "  Trained model:     $TRAIN_OUTPUT"
fi
echo ""
