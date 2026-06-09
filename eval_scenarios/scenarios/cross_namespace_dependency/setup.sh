#!/usr/bin/env bash
set -euo pipefail
FIXTURE_DIR="$(cd "$(dirname "$0")/fixtures" && pwd)"

oc apply -f "$FIXTURE_DIR/manifest.yaml"

ATTEMPT=0
until [ "$ATTEMPT" -ge 30 ]; do
  ATTEMPT=$((ATTEMPT + 1))
  if oc logs -l app=inventory-api -n app-frontend --tail=5 2>/dev/null | grep -q "FATAL"; then
    echo "Cross-namespace failure detected (attempt $ATTEMPT)"
    exit 0
  fi
  sleep 3
done
echo "Expected failure not detected"
exit 1
