#!/usr/bin/env bash
set -euo pipefail

# Sweep evaluation across ITS budgets and voting modes.
#
# Runs run_eval.sh once per (budget, voting_mode) configuration.
# Budget=1 is a true baseline (no ITS gateway) and runs only once.
#
# Usage:
#   ./run_its_sweep.sh <model_url> <model_name> [iterations]
#
# Options (env vars):
#   BUDGETS      Space-separated budgets (default: "1 4 8")
#   VOTE_MODES   Space-separated voting modes (default: "tool_hierarchical tool_flat_all")
#   JUDGE_MODEL  Judge LLM (default: gpt-5-mini)
#   TRACING      Enable Langfuse tracing: "on" or "off" (default: off)
#   DRY_RUN      Set to 1 to print commands without executing
#
# Examples:
#   ./run_its_sweep.sh https://api.openai.com/v1 gpt-5-mini 3
#   BUDGETS="1 4" VOTE_MODES="tool_flat_all" ./run_its_sweep.sh https://api.openai.com/v1 gpt-5-mini 1
#   DRY_RUN=1 ./run_its_sweep.sh https://api.openai.com/v1 gpt-5-mini 10

MODEL_URL="${1:?Usage: $0 <model_url> <model_name> [iterations]}"
MODEL_NAME="${2:?Usage: $0 <model_url> <model_name> [iterations]}"
ITERATIONS="${3:-3}"

BUDGETS="${BUDGETS:-1 4 8}"
VOTE_MODES="${VOTE_MODES:-tool_hierarchical tool_flat_all}"
JUDGE_MODEL="${JUDGE_MODEL:-gpt-5-mini}"
TRACING="${TRACING:-off}"
DRY_RUN="${DRY_RUN:-0}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/eval_scenarios/results"

cleanup() {
    echo ""
    echo "Sweep interrupted. Cleaning up..."
    pkill -f "runner.py" 2>/dev/null || true
    pkill -f "openshift-mcp-server" 2>/dev/null || true
    pkill -f "iaas" 2>/dev/null || true
    pkill -f "obs-mcp" 2>/dev/null || true
    exit 1
}
trap cleanup INT TERM

MODEL_SLUG=$(echo "$MODEL_NAME" | tr '/' '_' | tr '.' '-')

baseline_done=false
declare -A RUN_LABELS
declare -A RUN_TIMES

echo "=========================================="
echo "  ITS Eval Sweep"
echo "=========================================="
echo "  Model:      $MODEL_NAME @ $MODEL_URL"
echo "  Budgets:    $BUDGETS"
echo "  Vote modes: $VOTE_MODES"
echo "  Iterations: $ITERATIONS"
echo "  Judge:      $JUDGE_MODEL"
echo "  $(date)"
echo "=========================================="
echo ""

run_count=0

for vote_mode in $VOTE_MODES; do
    for budget in $BUDGETS; do
        # Budget=1 means no scaling — vote mode is irrelevant, run once
        if [ "$budget" = "1" ]; then
            if [ "$baseline_done" = true ]; then
                echo "Skipping budget=1 for $vote_mode (baseline already covered)"
                echo ""
                continue
            fi
            baseline_done=true
            label="${MODEL_SLUG}_baseline"
        else
            short_vote="${vote_mode#tool_}"
            label="${MODEL_SLUG}_its${budget}_${short_vote}"
        fi

        run_count=$((run_count + 1))

        echo "########################################"
        echo "  Run $run_count: budget=$budget vote=$vote_mode"
        echo "  Label: $label"
        echo "########################################"
        echo ""

        if [ "$DRY_RUN" = "1" ]; then
            if [ "$budget" = "1" ]; then
                echo "[dry-run] JUDGE_MODEL=$JUDGE_MODEL TRACING=$TRACING \\"
                echo "  ./run_eval.sh $label $MODEL_URL $MODEL_NAME $ITERATIONS"
            else
                echo "[dry-run] ITS_BUDGET=$budget ITS_ALGORITHM=self-consistency ITS_TOOL_VOTE=$vote_mode \\"
                echo "  JUDGE_MODEL=$JUDGE_MODEL TRACING=$TRACING \\"
                echo "  ./run_eval.sh $label $MODEL_URL $MODEL_NAME $ITERATIONS"
            fi
            echo ""
            RUN_LABELS[$run_count]="$label"
            RUN_TIMES[$run_count]="--"
            continue
        fi

        start_time=$(date +%s)
        rc=0

        if [ "$budget" = "1" ]; then
            JUDGE_MODEL="$JUDGE_MODEL" \
            TRACING="$TRACING" \
                "$SCRIPT_DIR/run_eval.sh" "$label" "$MODEL_URL" "$MODEL_NAME" "$ITERATIONS" || rc=$?
        else
            ITS_BUDGET="$budget" \
            ITS_ALGORITHM="self-consistency" \
            ITS_TOOL_VOTE="$vote_mode" \
            JUDGE_MODEL="$JUDGE_MODEL" \
            TRACING="$TRACING" \
                "$SCRIPT_DIR/run_eval.sh" "$label" "$MODEL_URL" "$MODEL_NAME" "$ITERATIONS" || rc=$?
        fi

        end_time=$(date +%s)
        elapsed=$(( end_time - start_time ))
        elapsed_fmt="$(( elapsed / 3600 ))h $(( (elapsed % 3600) / 60 ))m $(( elapsed % 60 ))s"

        RUN_LABELS[$run_count]="$label"
        if [ "$rc" -ne 0 ]; then
            RUN_TIMES[$run_count]="FAILED (${elapsed_fmt})"
            echo ""
            echo "WARNING: Run $run_count failed (exit code $rc) after $elapsed_fmt"
            echo ""
        else
            RUN_TIMES[$run_count]="$elapsed_fmt"
            echo ""
            echo "Run $run_count complete ($elapsed_fmt)"
            echo ""
        fi
    done
done

echo ""
echo "=========================================="
echo "  Sweep Complete"
echo "  $(date)"
echo "=========================================="
echo ""

# Print comparison table
(for i in $(seq 1 $run_count); do
    echo "${RUN_LABELS[$i]},${RUN_TIMES[$i]}"
done) | python3 -c "
import csv, glob, sys

results_dir = sys.argv[1]
runs = []
for line in sys.stdin:
    line = line.strip()
    if line:
        label, elapsed = line.split(',', 1)
        runs.append((label, elapsed))

fmt = '{:<40} {:>16} {:>12} {:>8} {:>14}'
print(fmt.format('Config', 'Pass Rate', 'Avg Score', 'Errors', 'Elapsed'))
print('-' * 92)

for label, elapsed in runs:
    path = f'{results_dir}/traced_{label}'
    passed = failed = errors = 0
    scores = []
    for i in range(1, 100):
        found = False
        for f in sorted(glob.glob(f'{path}/iter_{i:02d}/*/*detailed*.csv')):
            found = True
            for row in csv.DictReader(open(f)):
                r = row.get('result', '')
                if r == 'PASS': passed += 1
                elif r == 'FAIL': failed += 1
                elif r == 'ERROR': errors += 1
                s = row.get('score', '')
                if s:
                    try: scores.append(float(s))
                    except ValueError: pass
        if not found:
            break

    judged = passed + failed
    if judged > 0:
        rate = f'{passed}/{judged} ({passed/judged*100:.1f}%)'
        avg_score = f'{sum(scores)/len(scores):.3f}' if scores else 'n/a'
        err_str = str(errors) if errors else '-'
    else:
        rate = 'no data'
        avg_score = 'n/a'
        err_str = str(errors) if errors else '-'

    print(fmt.format(label, rate, avg_score, err_str, elapsed))

print()
" "$RESULTS_DIR"
