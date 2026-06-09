#!/usr/bin/env bash
set -euo pipefail

# Evaluate a model on the OLS troubleshooting benchmark.
#
# Usage:
#   ./run_eval.sh <model_label> <model_url> <model_name> [iterations]
#
# Args:
#   run_name       Unique name for this eval run. Results go to eval_scenarios/results/traced_<run_name>/
#                  Use descriptive names to distinguish runs (e.g. gpt5mini_gpt5judge, nemotron_sft_3iter)
#   model_url      OpenAI-compatible API base URL (e.g. http://localhost:8234/v1)
#   model_name     Model name to send in API requests (e.g. openshift-expert, gpt-5-mini)
#   iterations     Number of eval iterations (default: 3)
#
# Options (env vars):
#   ITER_OFFSET    Starting iteration offset (default: 0)
#   JUDGE_MODEL    Judge LLM for scoring (default: gpt-5-mini)
#   TRACING        Enable Langfuse tracing: "on" or "off" (default: off)
#   OLS_DIR        Path to lightspeed-service (default: ./lightspeed-service)
#   EVAL_CLI       Path to lightspeed-eval binary (auto-detected)
#
# Examples:
#   ./run_eval.sh gpt5mini_run1 https://api.openai.com/v1 gpt-5-mini 1
#   ./run_eval.sh nemotron_base http://localhost:8234/v1 openshift-expert 3
#   TRACING=on ./run_eval.sh nemotron_sft http://localhost:8250/v1 nemotron-gpt55-sft 5
#   JUDGE_MODEL=gpt-4.1 ./run_eval.sh gpt5mini_41judge https://api.openai.com/v1 gpt-5-mini 3

MODEL_LABEL="${1:?Usage: $0 <run_name> <model_url> <model_name> [iterations]}"
MODEL_URL="${2:?Usage: $0 <run_name> <model_url> <model_name> [iterations]}"
MODEL_NAME="${3:?Usage: $0 <run_name> <model_url> <model_name> [iterations]}"
ITERATIONS="${4:-${ITERATIONS:-3}}"

ITER_OFFSET="${ITER_OFFSET:-0}"
JUDGE_MODEL="${JUDGE_MODEL:-gpt-5-mini}"
TRACING="${TRACING:-off}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OLS_DIR="${OLS_DIR:-$SCRIPT_DIR/lightspeed-service}"
EVAL_DIR="$SCRIPT_DIR/eval_scenarios"
EVAL_CLI="${EVAL_CLI:-$(cd "$OLS_DIR" && uv run which lightspeed-eval 2>/dev/null || command -v lightspeed-eval 2>/dev/null || echo "")}"
WORK_DIR="$SCRIPT_DIR/.work/$MODEL_LABEL"

if [ ! -d "$OLS_DIR" ]; then echo "ERROR: lightspeed-service not found at $OLS_DIR. Run: bash setup.sh"; exit 1; fi
if [ -z "$EVAL_CLI" ]; then echo "ERROR: lightspeed-eval not found. Run: bash setup.sh"; exit 1; fi
if ! oc whoami &>/dev/null; then echo "ERROR: not logged into cluster. Run: oc login ..."; exit 1; fi

if [ -f "$SCRIPT_DIR/.env" ]; then set -a && source "$SCRIPT_DIR/.env" && set +a; fi
export LANGSMITH_API_KEY="${LANGSMITH_API_KEY:-unused}"
export LANGCHAIN_TRACING_V2="${LANGCHAIN_TRACING_V2:-false}"

if [ "$TRACING" = "off" ]; then
    export LANGFUSE_SECRET_KEY=""
    export LANGFUSE_PUBLIC_KEY=""
    export LANGFUSE_HOST=""
fi

if [[ "$MODEL_URL" == *"openai.com"* ]]; then PROVIDER_TYPE="openai"; else PROVIDER_TYPE="rhoai_vllm"; fi

mkdir -p "$WORK_DIR"

cat > "$WORK_DIR/olsconfig.yaml" << EOF
llm_providers:
  - name: my_openai
    type: ${PROVIDER_TYPE}
    url: "${MODEL_URL}"
    credentials_path: ${SCRIPT_DIR}/.openai_key
    models:
      - name: ${MODEL_NAME}
        context_window_size: 32768

mcp_servers:
  - name: openshift-mcp-server
    url: 'http://127.0.0.1:8085/mcp'
    headers:
      Authorization: kubernetes
    timeout: 30

ols_config:
  conversation_cache:
    type: memory
    memory:
      max_entries: 1000
  default_provider: my_openai
  default_model: ${MODEL_NAME}
  authentication_config:
    module: "noop-with-token"
  user_data_collection:
    feedback_disabled: true
    feedback_storage: "/tmp/ols-eval/feedback"
    transcripts_disabled: true
    transcripts_storage: "/tmp/ols-eval/transcripts"

dev_config:
  enable_dev_ui: true
  disable_auth: false
  disable_tls: true
  uvicorn_port_number: 8080
EOF

sed "s|model: \"openshift-expert\"|model: \"${MODEL_NAME}\"|; s|model: \"gpt-5-mini\"|model: \"${JUDGE_MODEL}\"|" \
    "$EVAL_DIR/system_qwen35_9b.yaml" > "$WORK_DIR/system.yaml"

if [ ! -f "$SCRIPT_DIR/.openai_key" ] && [ -n "${OPENAI_API_KEY:-}" ]; then
    echo "$OPENAI_API_KEY" > "$SCRIPT_DIR/.openai_key"
fi

OUTPUT_BASE="$EVAL_DIR/results/traced_${MODEL_LABEL}"

MCP_SERVER="${MCP_SERVER:-/mnt/vde/workspace/vpcuser/tmp/openshift-mcp-server}"
MCP_CONFIG="${MCP_CONFIG:-$SCRIPT_DIR/mcp_config.toml}"
pkill -f "openshift-mcp-server" 2>/dev/null || true; sleep 2
"$MCP_SERVER" --port 8085 ${MCP_CONFIG:+--config "$MCP_CONFIG"} > "$WORK_DIR/mcp.log" 2>&1 &
sleep 3

cd "$OLS_DIR"
pkill -f "runner.py" 2>/dev/null || true; sleep 2
EVAL_MODEL_LABEL="$MODEL_LABEL" \
OLS_CONFIG_FILE="$WORK_DIR/olsconfig.yaml" \
uv run python runner.py > "$WORK_DIR/ols.log" 2>&1 &

for i in $(seq 1 30); do
    resp=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/readiness 2>/dev/null || echo "none")
    if [ "$resp" = "200" ]; then echo "OLS ready (attempt $i)"; break; fi
    if [ "$i" = "30" ]; then echo "ERROR: OLS failed to start. Check $WORK_DIR/ols.log"; exit 1; fi
    sleep 3
done

mkdir -p "$OUTPUT_BASE"

TAGS=(envvar_missing batch_failure storage_binding namespace_pod_count \
      scheduled_outage_detection periodic_failure_window \
      readiness_probe_diagnosis ingress_rule_mismatch oom wrong_networkpolicy \
      config_drift_analysis)

echo "========================================="
echo "  Model:      $MODEL_NAME @ $MODEL_URL"
echo "  Label:      $MODEL_LABEL"
echo "  Judge:      $JUDGE_MODEL"
echo "  Iterations: $ITERATIONS (offset $ITER_OFFSET)"
echo "  Tracing:    $TRACING"
echo "  Results:    $OUTPUT_BASE"
echo "  $(date)"
echo "========================================="

for iter in $(seq 1 $ITERATIONS); do
    actual_iter=$((iter + ITER_OFFSET))
    echo ""
    echo "=== Iteration $actual_iter (run $iter/$ITERATIONS) ==="

    for tag in "${TAGS[@]}"; do
        echo "--- $tag ---"
        printf "scenario=%s\niteration=%s\ncheckpoint=%s\n" "$tag" "$actual_iter" "$MODEL_LABEL" > /tmp/eval_context.txt

        scenario_dir="$EVAL_DIR/scenarios/$tag"

        [ -f "$scenario_dir/cleanup.sh" ] && bash "$scenario_dir/cleanup.sh" 2>/dev/null || true
        sleep 3

        ns_list=$(grep -hE '^NS=|^NS_[AB]=' "$scenario_dir"/setup.sh 2>/dev/null | sed 's/^NS[_AB]*="//' | sed 's/"//')
        for ns in $(echo "$ns_list" | sort -u | grep -v '^$'); do
            oc create namespace "$ns" 2>/dev/null || true
            if [ -n "${DOCKERHUB_USER:-}" ] && [ -n "${DOCKERHUB_TOKEN:-}" ]; then
                oc create secret docker-registry dockerhub \
                    --docker-server=docker.io \
                    --docker-username="$DOCKERHUB_USER" \
                    --docker-password="$DOCKERHUB_TOKEN" \
                    -n "$ns" --dry-run=client -o yaml 2>/dev/null | oc apply -f - 2>/dev/null
                oc secrets link default dockerhub --for=pull -n "$ns" 2>/dev/null
            fi
        done

        [ -f "$scenario_dir/setup.sh" ] && bash "$scenario_dir/setup.sh" 2>&1 | tail -1 || echo "WARN: setup"

        ITER_DIR="$OUTPUT_BASE/iter_$(printf '%02d' $actual_iter)/$tag"
        mkdir -p "$ITER_DIR"

        cd "$OLS_DIR"
        API_KEY=$(oc whoami -t) uv run $EVAL_CLI \
            --system-config "$WORK_DIR/system.yaml" \
            --eval-data "$EVAL_DIR/evals.yaml" \
            --output-dir "$ITER_DIR" \
            --tags "$tag" 2>&1 | grep -E "Pass|Fail|Error|Complete" || true

        if [ -f "$scenario_dir/cleanup.sh" ]; then
            bash "$scenario_dir/cleanup.sh" 2>/dev/null || true
        fi
        sleep 3
        echo "Done: $tag"
    done
    echo "Iteration $actual_iter complete"
done

echo ""
echo "========================================="
echo "  Eval complete: $MODEL_LABEL"
echo "  $(date)"
echo "========================================="

python3 -c "
import csv, glob
path = '$OUTPUT_BASE'
total_all = p_all = 0
for i in range(1, 100):
    t = p = 0
    for f in sorted(glob.glob(f'{path}/iter_{i:02d}/*/*detailed*.csv')):
        for row in csv.DictReader(open(f)):
            t += 1
            if row.get('result') == 'PASS': p += 1
    if t > 0:
        total_all += t; p_all += p
        print(f'iter_{i:02d}: {p}/{t} = {p/t*100:.1f}%')
    else: break
if total_all > 0:
    print(f'TOTAL: {p_all}/{total_all} = {p_all/total_all*100:.1f}%')
"

pkill -f "runner.py" 2>/dev/null || true
pkill -f "openshift-mcp-server" 2>/dev/null || true
