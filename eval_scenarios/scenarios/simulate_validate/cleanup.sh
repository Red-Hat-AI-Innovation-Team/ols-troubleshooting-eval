#!/usr/bin/env bash
set -euo pipefail

oc delete -f "$(cd "$(dirname "$0")/fixtures" && pwd)/deployment.yaml" --ignore-not-found --wait=false
oc delete namespace simulate-test --ignore-not-found

# Clean up any ephemeral simulate clusters
podman rm -f factory-simulate-microshift 2>/dev/null || true
podman volume rm microshift-data 2>/dev/null || true
