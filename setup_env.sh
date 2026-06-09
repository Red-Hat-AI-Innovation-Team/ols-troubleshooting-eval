#!/bin/bash
set -euo pipefail

# setup_env.sh — Full eval environment setup (idempotent)
#
# Sets up: Docker Hub cache, CRC, CRC mirror, MCP server, cluster login.
# Safe to run multiple times — skips steps that are already done.
#
# Prerequisites:
#   - podman, crc, go (1.24+), oc installed
#   - .env file with DOCKERHUB_USER and DOCKERHUB_TOKEN
#   - .openai_key file with OpenAI API key
#   - ~/.crc/pull-secret.json
#
# Usage:
#   bash setup.sh          # install OLS + eval deps (one-time)
#   bash setup_env.sh      # set up CRC + cache + MCP (this script)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CACHE_DIR="${CACHE_DIR:-/mnt/nvme0n1/registry-cache}"
CACHE_PORT=5000
CACHE_NAME="dockerhub-cache"
MCP_BIN="${MCP_SERVER:-$SCRIPT_DIR/.work/openshift-mcp-server}"
MCP_BUILD_DIR="/tmp/openshift-mcp-server-build"
CRC_SSH_KEY="$HOME/.crc/machines/crc/id_ed25519"
MIRROR_CONF="099-dockerhub-mirror.conf"

SCENARIO_IMAGES=(
    "library/python:3.11-alpine"
    "library/python:3.9-slim"
    "library/busybox:1.36"
    "library/memcached:1.6-alpine"
    "library/alpine:3.19"
    "nginxinc/nginx-unprivileged:alpine"
    "nginxinc/nginx-unprivileged:latest"
)

log() { echo ""; echo "=== $1 ==="; }
ok()  { echo "  ✓ $1"; }
skip() { echo "  → $1 (already done)"; }

# ---------- Load credentials ----------

if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a && source "$SCRIPT_DIR/.env" && set +a
fi

# ---------- Check prerequisites ----------

log "Checking prerequisites"

for cmd in podman crc oc; do
    command -v "$cmd" &>/dev/null && ok "$cmd" || { echo "ERROR: $cmd not found"; exit 1; }
done

GO_BIN="${GO_BIN:-$(command -v go 2>/dev/null || echo /usr/local/go/bin/go)}"
"$GO_BIN" version &>/dev/null && ok "go ($("$GO_BIN" version | awk '{print $3}'))" || { echo "ERROR: go not found"; exit 1; }

[ -n "${DOCKERHUB_USER:-}" ] && [ -n "${DOCKERHUB_TOKEN:-}" ] && ok "Docker Hub credentials" || { echo "ERROR: Set DOCKERHUB_USER and DOCKERHUB_TOKEN in .env"; exit 1; }
[ -f "$SCRIPT_DIR/.openai_key" ] && ok "OpenAI key" || { echo "ERROR: Create .openai_key file"; exit 1; }
[ -f "$HOME/.crc/pull-secret.json" ] && ok "Pull secret" || { echo "WARN: ~/.crc/pull-secret.json not found (CRC start may fail)"; }

# ---------- Step 1: Docker Hub pull-through cache ----------

log "Step 1/5: Docker Hub pull-through cache"

if podman ps --format "{{.Names}}" 2>/dev/null | grep -q "^${CACHE_NAME}$"; then
    skip "Cache already running on port $CACHE_PORT"
else
    podman rm -f "$CACHE_NAME" 2>/dev/null || true
    mkdir -p "$CACHE_DIR"

    podman run -d \
        --name "$CACHE_NAME" \
        --restart always \
        -p "${CACHE_PORT}:5000" \
        -v "${CACHE_DIR}:/var/lib/registry" \
        -e "REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io" \
        -e "REGISTRY_PROXY_USERNAME=${DOCKERHUB_USER}" \
        -e "REGISTRY_PROXY_PASSWORD=${DOCKERHUB_TOKEN}" \
        -e "REGISTRY_STORAGE_DELETE_ENABLED=true" \
        docker.io/library/registry:2 > /dev/null

    sleep 3
    curl -sf "http://localhost:${CACHE_PORT}/v2/" > /dev/null && ok "Cache started" || { echo "ERROR: Cache failed to start"; exit 1; }
fi

# Pre-warm images
CATALOG=$(curl -sf "http://localhost:${CACHE_PORT}/v2/_catalog" 2>/dev/null || echo "{}")
WARMED=0
for img in "${SCENARIO_IMAGES[@]}"; do
    if echo "$CATALOG" | grep -q "$(echo "$img" | cut -d: -f1)"; then
        continue
    fi
    podman pull --tls-verify=false "localhost:${CACHE_PORT}/$img" > /dev/null 2>&1 && WARMED=$((WARMED + 1))
done
[ "$WARMED" -gt 0 ] && ok "Pre-warmed $WARMED images" || skip "All ${#SCENARIO_IMAGES[@]} images already cached"

# ---------- Step 2: CRC ----------

log "Step 2/5: CRC cluster"

CRC_STATUS=$(crc status 2>/dev/null | grep "CRC VM:" | awk '{print $3}' || echo "Stopped")

if [ "$CRC_STATUS" = "Running" ]; then
    skip "CRC already running"
else
    # Ensure host-network-access is enabled
    if ! crc config view 2>/dev/null | grep -q "host-network-access.*true"; then
        crc config set host-network-access true
        echo "  Set host-network-access=true"
        # Need cleanup+setup if config changed
        crc cleanup 2>/dev/null || true
        crc setup 2>/dev/null
    fi

    echo "  Starting CRC (this takes a few minutes)..."
    crc start --pull-secret-file "$HOME/.crc/pull-secret.json"
    ok "CRC started"
fi

# ---------- Step 3: CRC mirror config ----------

log "Step 3/5: CRC Docker Hub mirror"

SSH="ssh -i $CRC_SSH_KEY -p 2222 -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o LogLevel=ERROR core@127.0.0.1"

MIRROR_EXISTS=$($SSH "test -f /etc/containers/registries.conf.d/${MIRROR_CONF} && echo yes || echo no" 2>/dev/null || echo "no")

if [ "$MIRROR_EXISTS" = "yes" ]; then
    skip "Mirror already configured"
else
    $SSH "sudo tee /etc/containers/registries.conf.d/${MIRROR_CONF} > /dev/null" << EOF
[[registry]]
prefix = "docker.io"
location = "docker.io"

[[registry.mirror]]
location = "host.crc.testing:${CACHE_PORT}"
insecure = true
EOF
    $SSH "sudo systemctl restart crio" 2>/dev/null
    sleep 5
    ok "Mirror configured, CRI-O restarted"
fi

# Verify connectivity
$SSH "curl -sf --max-time 5 http://host.crc.testing:${CACHE_PORT}/v2/ > /dev/null" 2>/dev/null \
    && ok "Cache reachable from CRC" \
    || echo "  WARN: Cache not reachable from CRC VM"

# ---------- Step 4: MCP server ----------

log "Step 4/5: Go MCP server"

if [ -x "$MCP_BIN" ]; then
    skip "Binary exists at $MCP_BIN"
else
    echo "  Building openshift-mcp-server..."
    if [ ! -d "$MCP_BUILD_DIR" ]; then
        git clone --depth 1 https://github.com/openshift/openshift-mcp-server.git "$MCP_BUILD_DIR"
    fi
    mkdir -p "$(dirname "$MCP_BIN")"
    (cd "$MCP_BUILD_DIR" && "$GO_BIN" build -o "$MCP_BIN" ./cmd/kubernetes-mcp-server/)
    ok "Built at $MCP_BIN"
fi

# ---------- Step 5: Cluster login ----------

log "Step 5/5: Cluster login"

eval $(crc oc-env 2>/dev/null) || true

KUBEADMIN_PASS=$(cat "$HOME/.crc/machines/crc/kubeadmin-password" 2>/dev/null || echo "")
if [ -n "$KUBEADMIN_PASS" ]; then
    oc login -u kubeadmin -p "$KUBEADMIN_PASS" \
        https://api.crc.testing:6443 --insecure-skip-tls-verify=true > /dev/null 2>&1 \
        && ok "Logged in as kubeadmin" \
        || echo "  WARN: Login failed"
else
    echo "  WARN: kubeadmin password not found, login manually"
fi

# ---------- Summary ----------

echo ""
echo "=========================================="
echo "  Environment Ready"
echo "=========================================="
echo ""
echo "  Cache:      http://localhost:${CACHE_PORT} (${CACHE_DIR})"
echo "  CRC:        $(crc status 2>/dev/null | grep 'CRC VM:' || echo 'unknown')"
echo "  MCP server: ${MCP_BIN}"
echo "  Cluster:    $(oc whoami 2>/dev/null || echo 'not logged in') @ $(oc whoami --show-server 2>/dev/null || echo 'unknown')"
echo ""
echo "  Run an eval:"
echo "    export OPENAI_API_KEY=\$(cat .openai_key)"
echo "    ./run_eval.sh <label> <model_url> <model_name> [iterations]"
echo ""
