# OLS Troubleshooting Eval — Complete Setup Guide

End-to-end setup for running the OLS troubleshooting benchmark on CRC
with a Docker Hub pull-through cache and the Go `openshift-mcp-server`.

## Prerequisites

- CRC (OpenShift Local) installed (`crc version`)
- `podman` installed
- `oc` CLI installed
- Go 1.24+ at `/usr/local/go/bin/go` (for building MCP server)
- OpenAI API key
- Docker Hub credentials (username + PAT)
- Red Hat pull secret at `~/.crc/pull-secret.json`

## One-time setup

### 1. Clone and install dependencies

```bash
cd /mnt/vde/workspace/vpcuser/ols-troubleshooting-eval
bash setup.sh
```

This clones `lightspeed-service`, installs deps, and sets up the `lightspeed-eval` CLI.

### 2. Configure credentials

```bash
# .env file
cat > .env << EOF
DOCKERHUB_USER=<your_dockerhub_username>
DOCKERHUB_TOKEN=<your_dockerhub_pat>
EOF

# OpenAI key
echo "<your_openai_key>" > .openai_key
export OPENAI_API_KEY=$(cat .openai_key)
```

### 3. Start Docker Hub pull-through cache

Runs a local registry on the host that transparently caches Docker Hub
images. Survives CRC stop/start/delete — data persists on NVMe.

```bash
podman run -d \
    --name dockerhub-cache \
    --restart always \
    -p 5000:5000 \
    -v /mnt/nvme0n1/registry-cache:/var/lib/registry \
    -e "REGISTRY_PROXY_REMOTEURL=https://registry-1.docker.io" \
    -e "REGISTRY_PROXY_USERNAME=$DOCKERHUB_USER" \
    -e "REGISTRY_PROXY_PASSWORD=$DOCKERHUB_TOKEN" \
    docker.io/library/registry:2

# Verify
curl -sf http://localhost:5000/v2/ && echo "Cache running"
```

Pre-warm all scenario images (one-time, avoids Docker Hub pulls during eval):

```bash
for img in library/python:3.11-alpine library/python:3.9-slim \
           library/busybox:1.36 library/memcached:1.6-alpine \
           library/alpine:3.19 nginxinc/nginx-unprivileged:alpine \
           nginxinc/nginx-unprivileged:latest; do
    podman pull --tls-verify=false localhost:5000/$img
done
```

### 4. Start CRC with host network access

```bash
crc config set host-network-access true
crc cleanup && crc setup
crc start --pull-secret-file ~/.crc/pull-secret.json
```

`host-network-access` must be set before `crc start` so the CRC VM
can reach the pull-through cache on the host.

### 5. Configure CRC to mirror Docker Hub

SSH into the CRC VM and add a CRI-O mirror drop-in:

```bash
SSH="ssh -i ~/.crc/machines/crc/id_ed25519 -p 2222 -o StrictHostKeyChecking=no core@127.0.0.1"

$SSH "sudo tee /etc/containers/registries.conf.d/099-dockerhub-mirror.conf > /dev/null" << 'EOF'
[[registry]]
prefix = "docker.io"
location = "docker.io"

[[registry.mirror]]
location = "host.crc.testing:5000"
insecure = true
EOF

$SSH "sudo systemctl restart crio"
```

Verify from inside CRC:

```bash
$SSH "curl -sf http://host.crc.testing:5000/v2/ && echo 'mirror reachable'"
```

This step must be repeated after `crc delete` + `crc start` (mirror config
lives inside the VM). The cache data on the host persists.

### 6. Build the Go MCP server

```bash
git clone --depth 1 https://github.com/openshift/openshift-mcp-server.git /tmp/mcp-build
cd /tmp/mcp-build
/usr/local/go/bin/go build -o /mnt/vde/workspace/vpcuser/tmp/openshift-mcp-server ./cmd/kubernetes-mcp-server/
```

### 7. Login to cluster

```bash
eval $(crc oc-env)
oc login -u kubeadmin -p $(cat ~/.crc/machines/crc/kubeadmin-password) \
    https://api.crc.testing:6443 --insecure-skip-tls-verify=true
```

## Running an eval

```bash
cd /mnt/vde/workspace/vpcuser/ols-troubleshooting-eval
export OPENAI_API_KEY=$(cat .openai_key)

# Eval gpt-5-mini, 3 iterations
./run_eval.sh gpt5mini_run1 https://api.openai.com/v1 gpt-5-mini 3

# Eval a local model (e.g. vLLM on port 8234)
./run_eval.sh qwen3_4b http://localhost:8234/v1 openshift-expert 3

# Use a stronger judge
JUDGE_MODEL=gpt-4.1 ./run_eval.sh gpt5mini_41judge https://api.openai.com/v1 gpt-5-mini 3
```

Results go to `eval_scenarios/results/traced_<label>/`.

## What happens during an eval run

1. Go `openshift-mcp-server` starts on port 8085 (host-side, read-write)
2. `lightspeed-service` starts on port 8080 (host-side, unmodified upstream)
3. For each of 11 scenarios per iteration:
   - Cleanup previous scenario
   - Create namespace + Docker Hub pull secret
   - Deploy broken workload (`setup.sh`)
   - `lightspeed-eval` sends query to OLS → OLS calls MCP tools → MCP queries cluster → judge scores response
   - Cleanup scenario
4. Summary printed: pass rate per iteration + total
5. OLS + MCP killed

## After `crc delete`

The pull-through cache on the host survives. After `crc start` on a fresh VM:

1. Re-run step 5 (configure CRI-O mirror)
2. Re-run step 7 (login to cluster)
3. Ready to eval — images served from cache, no Docker Hub pulls

## Key files

| File | Purpose |
|------|---------|
| `run_eval.sh` | Main eval script — starts services, loops scenarios, prints results |
| `mcp_config.toml` | Go MCP server config (denied resources, disabled tools) |
| `eval_scenarios/evals.yaml` | 11 scenario definitions with queries, expected responses, metrics |
| `eval_scenarios/scenarios/*/setup.sh` | Per-scenario deployment scripts |
| `eval_scenarios/scenarios/*/cleanup.sh` | Per-scenario teardown scripts |
| `eval_scenarios/system_*.yaml` | Judge + metrics config variants |
| `.env` | Docker Hub credentials |
| `.openai_key` | OpenAI API key |

## Differences from Eshwar's original

1. **MCP server**: Go `openshift-mcp-server` (read-write, port 8085) instead of npm `kubernetes-mcp-server` (read-only, port 8089). Matches production OLS.
2. **config_drift setup**: `oc wait` + single log check instead of 80-iteration polling loop.
3. **Docker Hub images**: Pull-through cache instead of direct Docker Hub pulls. No scenario manifest changes.
4. **Cluster**: CRC instead of pawshift. Scenarios are cluster-agnostic.
