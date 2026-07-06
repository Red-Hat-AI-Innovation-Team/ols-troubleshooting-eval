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
p_all = f_all = e_all = 0

for i in range(1, 100):
    p = f = e = 0
    for fp in sorted(glob.glob(f'{path}/iter_{i:02d}/*/*detailed*.csv')):
        scenario = fp.split('/')[-2]
        for row in csv.DictReader(open(fp)):
            r = row.get('result', '')
            if scenario not in scenarios:
                scenarios[scenario] = {'p': 0, 'f': 0, 'e': 0}
            if r == 'PASS': p += 1; scenarios[scenario]['p'] += 1
            elif r == 'FAIL': f += 1; scenarios[scenario]['f'] += 1
            elif r == 'ERROR': e += 1; scenarios[scenario]['e'] += 1
    judged = p + f
    if judged + e > 0:
        p_all += p; f_all += f; e_all += e
        parts = [f'{p}/{judged} = {p/judged*100:.1f}%' if judged else '0/0']
        if e: parts.append(f'{e} errors')
        print(f'iter_{i:02d}: {\"  \".join(parts)}')
    else:
        break

judged_all = p_all + f_all
if judged_all > 0:
    parts = [f'{p_all}/{judged_all} = {p_all/judged_all*100:.1f}%']
    if e_all: parts.append(f'{e_all} errors')
    print(f'TOTAL: {\"  \".join(parts)}')
    print()
    print('Per scenario:')
    for s in sorted(scenarios):
        d = scenarios[s]
        judged = d['p'] + d['f']
        parts = [f'{d[\"p\"]}/{judged} = {d[\"p\"]/judged*100:.0f}%' if judged else '0/0']
        if d['e']: parts.append(f'{d[\"e\"]}err')
        print(f'  {s:<30} {\"  \".join(parts)}')
"
