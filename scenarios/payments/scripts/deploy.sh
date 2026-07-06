#!/bin/bash
set -e

echo "=== Enabling user workload monitoring ==="
oc apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-monitoring-config
  namespace: openshift-monitoring
data:
  config.yaml: |
    enableUserWorkload: true
EOF

echo ""
echo "=== Cleaning up existing namespaces ==="
oc delete namespace shared-services --ignore-not-found --wait
oc delete namespace payments --ignore-not-found --wait

echo ""
echo "=== Deploying shared-services ==="
oc apply -f manifests/shared-services/
oc -n shared-services wait --for=condition=available deployment/postgres --timeout=120s
oc -n shared-services wait --for=condition=available deployment/reporting-service --timeout=120s

echo ""
echo "=== Deploying payments ==="
oc apply -f manifests/payments/
oc -n payments wait --for=condition=available deployment/payments-api --timeout=120s

ROUTE=$(oc -n payments get route payments-api -o jsonpath='{.spec.host}')
echo ""
echo "Done. All pods running with v1.0.1."
echo "Swagger UI: http://${ROUTE}/docs"
echo "API:        http://${ROUTE}/api/v1/process-payment"
