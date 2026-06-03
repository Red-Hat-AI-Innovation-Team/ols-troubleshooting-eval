#!/usr/bin/env bash
set -euo pipefail
FIXTURE_DIR="$(cd "$(dirname "$0")/fixtures" && pwd)"
NS="order-processing"

oc apply -f "$FIXTURE_DIR/manifest.yaml"

ATTEMPT=0
until [ "$ATTEMPT" -ge 20 ]; do
  ATTEMPT=$((ATTEMPT + 1))
  if oc logs -l app=order-processor -n "$NS" --tail=5 2>/dev/null | grep -q "FATAL"; then
    echo "CrashLoop with FATAL detected (attempt $ATTEMPT)"
    exit 0
  fi
  sleep 3
done
echo "Expected crash not detected"
exit 1
