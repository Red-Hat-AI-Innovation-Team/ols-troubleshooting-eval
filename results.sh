#!/usr/bin/env bash
set -euo pipefail

# Print pass rates for a completed eval run.
#
# Usage:
#   ./results.sh <model_label>
#   ./results.sh              # lists available results

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/eval_scenarios/results"

if [ -z "${1:-}" ]; then
    echo "Available results:"
    for d in "$RESULTS_DIR"/traced_*/; do
        [ -d "$d" ] || continue
        label=$(basename "$d" | sed 's/traced_//')
        iters=$(ls -d "$d"/iter_* 2>/dev/null | wc -l | tr -d ' ')
        echo "  $label ($iters iterations)"
    done
    echo ""
    echo "Usage: $0 <model_label>"
    exit 0
fi

MODEL_LABEL="$1"
RESULT_PATH="$RESULTS_DIR/traced_${MODEL_LABEL}"

if [ ! -d "$RESULT_PATH" ]; then
    echo "ERROR: No results found for '$MODEL_LABEL' at $RESULT_PATH"
    exit 1
fi

python3 -c "
import csv, glob, os

path = '$RESULT_PATH'
scenarios = {}
total_all = p_all = 0

for i in range(1, 100):
    t = p = 0
    for f in sorted(glob.glob(f'{path}/iter_{i:02d}/*/*detailed*.csv')):
        scenario = f.split('/')[-2]
        for row in csv.DictReader(open(f)):
            t += 1
            r = row.get('result', '')
            if r == 'PASS': p += 1
            if scenario not in scenarios:
                scenarios[scenario] = {'p': 0, 'f': 0, 'e': 0}
            if r == 'PASS': scenarios[scenario]['p'] += 1
            elif r == 'FAIL': scenarios[scenario]['f'] += 1
            elif r == 'ERROR': scenarios[scenario]['e'] += 1
    if t > 0:
        total_all += t; p_all += p
        print(f'iter_{i:02d}: {p}/{t} = {p/t*100:.1f}%')
    else:
        break

if total_all > 0:
    print(f'TOTAL: {p_all}/{total_all} = {p_all/total_all*100:.1f}%')
    print()
    print('Per scenario:')
    for s in sorted(scenarios):
        d = scenarios[s]
        t = d['p'] + d['f'] + d['e']
        print(f'  {s:<30} {d[\"p\"]}/{t} = {d[\"p\"]/t*100:.0f}%')
"
