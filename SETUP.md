# OLS Troubleshooting Eval — Setup & Workflow

## Hardware Requirements

**Run CRC on a GPU node, not a laptop.** CRC runs a full OpenShift VM that needs significant resources. A laptop will struggle with CRC + OLS + MCP server + eval simultaneously.

Recommended node specs:
- 32+ CPU cores (CRC uses 16 by default)
- 64+ GB RAM (CRC uses 32GB by default)
- 100+ GB disk for CRC VM
- Fast NVMe storage for Docker Hub image cache

Our runs use nodes with 80 CPUs, 1.2TB RAM, and NVMe storage.

## Architecture

```
                                    ┌─────────────┐
                                    │  CRC (VM)   │
                                    │  OpenShift   │
                                    │  + broken    │
                                    │  scenarios   │
                                    └──────▲───────┘
                                           │ kubectl/oc
┌─────────────┐    ┌──────────────┐   ┌────┴─────────┐
│ lightspeed  │◄──►│ OLS          │──►│ openshift-   │
│ -eval       │    │ (port 8080)  │   │ mcp-server   │
│ (judge)     │    │              │   │ (port 8085)  │
└─────────────┘    └──────┬───────┘   └──────────────┘
                          │
              ┌───────────┴───────────┐
              │  Without ITS:         │
              │  OLS → LLM directly   │
              │                       │
              │  With ITS:            │
              │  OLS → ITS gateway    │
              │  (port 8100) → LLM    │
              └───────────────────────┘
```

All services run on the host, not in the cluster. Only the broken scenarios run inside CRC.

## Prerequisites

- CRC (OpenShift Local) installed
- `podman` installed
- Go 1.24+ (for building MCP server)
- OpenAI API key
- Docker Hub credentials (username + PAT)
- Red Hat pull secret at `~/.crc/pull-secret.json`

## Quick Start

```bash
git clone https://github.com/Red-Hat-AI-Innovation-Team/ols-troubleshooting-eval.git
cd ols-troubleshooting-eval
git checkout ols-eval-reconciliation

# 1. Install OLS + eval deps
make setup

# 2. Create credentials (manual, one-time)
cat > .env << EOF
DOCKERHUB_USER=<your_dockerhub_username>
DOCKERHUB_TOKEN=<your_dockerhub_pat>
EOF
echo "<your_openai_key>" > .openai_key

# 3. Set up CRC + cache + MCP server (automated, idempotent)
make env-up

# 4. Run eval
make eval ARGS="gpt5mini https://api.openai.com/v1 gpt-5-mini 1"

# 5. Teardown
make env-down       # stop CRC (fast restart later)
make env-nuke       # full cleanup (delete CRC VM + stop cache)
```

`make env-up` automates everything after credentials:

| Step | What it does | Skips if |
|------|-------------|----------|
| Docker Hub cache | Starts `registry:2` pull-through proxy on port 5000, pre-warms all scenario images | Container already running |
| CRC | Sets `host-network-access=true`, runs `crc setup` + `crc start` | CRC already running |
| CRC mirror | SSHs into CRC VM, writes CRI-O drop-in config, restarts CRI-O | Config file already exists |
| MCP server | Clones + builds Go `openshift-mcp-server` binary | Binary already exists |
| Cluster login | `oc login` as kubeadmin | — |

After `crc delete`, just run `make env-up` again — the cache data persists on the host, CRC gets recreated, and the mirror is reconfigured automatically.

---

## Detailed Setup (Reference)

The steps below explain what `make env-up` does internally. You don't need to run these manually — they're here so you understand the components.

### Docker Hub pull-through cache

Caches Docker Hub images locally so scenario deployments don't hit rate limits. Persists across CRC stop/start/delete.

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

# Pre-warm scenario images
for img in library/python:3.11-alpine library/python:3.9-slim \
           library/busybox:1.36 library/memcached:1.6-alpine \
           library/alpine:3.19 nginxinc/nginx-unprivileged:alpine \
           nginxinc/nginx-unprivileged:latest; do
    podman pull --tls-verify=false localhost:5000/$img
done
```

### 4. Start CRC

```bash
crc config set host-network-access true   # required for cache access
crc cleanup && crc setup
crc start --pull-secret-file ~/.crc/pull-secret.json
```

### 5. Configure CRC to use the cache

After every `crc start` on a fresh VM (not needed after `crc stop`/`crc start`):

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

### 6. Build Go MCP server

```bash
git clone --depth 1 https://github.com/openshift/openshift-mcp-server.git /tmp/mcp-build
cd /tmp/mcp-build
/usr/local/go/bin/go build -o /path/to/openshift-mcp-server ./cmd/kubernetes-mcp-server/
```

Set `MCP_SERVER` env var or place the binary where `run_eval.sh` expects it.

### 7. Login to cluster

```bash
eval $(crc oc-env)
oc login -u kubeadmin -p $(cat ~/.crc/machines/crc/kubeadmin-password) \
    https://api.crc.testing:6443 --insecure-skip-tls-verify=true
```

## Running an Eval

### Basic (no ITS)

```bash
export OPENAI_API_KEY=$(cat .openai_key)
./run_eval.sh <label> <model_url> <model_name> [iterations]

# Examples
./run_eval.sh gpt5mini https://api.openai.com/v1 gpt-5-mini 3
./run_eval.sh qwen3_4b http://localhost:8234/v1 openshift-expert 3
JUDGE_MODEL=gpt-4.1 ./run_eval.sh gpt5mini_41judge https://api.openai.com/v1 gpt-5-mini 3
```

### With Inference-Time Scaling (ITS)

ITS uses `its_hub`'s IaaS gateway as a drop-in proxy between OLS and the LLM. No OLS patching needed.

```bash
# Install its_hub (one-time, in the OLS venv)
cd lightspeed-service && uv pip install -e /path/to/its_hub

# Run with ITS budget=4
ITS_BUDGET=4 ./run_eval.sh gpt5mini_its4 https://api.openai.com/v1 gpt-5-mini 3

# Custom algorithm/voting
ITS_BUDGET=8 ITS_ALGORITHM=self-consistency ITS_TOOL_VOTE=tool_flat_all \
    ./run_eval.sh gpt5mini_its8_flat https://api.openai.com/v1 gpt-5-mini 3
```

When `ITS_BUDGET` is set, the script:
1. Starts the `its-iaas` gateway on port 8100
2. Configures it with the model endpoint, algorithm, and budget
3. Points OLS at the gateway instead of the LLM directly
4. OLS sends requests to the gateway, which makes N parallel LLM calls and majority-votes on tool calls
5. Returns the winner in OpenAI streaming format — OLS doesn't know ITS is happening

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `JUDGE_MODEL` | gpt-5-mini | Judge LLM for scoring |
| `ITER_OFFSET` | 0 | Skip completed iterations (for resuming) |
| `TRACING` | off | Enable Langfuse tracing |
| `ITS_BUDGET` | (unset) | ITS budget — set to enable ITS (e.g., 4, 8) |
| `ITS_ALGORITHM` | self-consistency | ITS algorithm |
| `ITS_TOOL_VOTE` | tool_hierarchical | Voting strategy |
| `MCP_SERVER` | (auto) | Path to openshift-mcp-server binary |

## What Happens During an Eval

1. Go `openshift-mcp-server` starts on port 8085 (host-side, read-write)
2. (If ITS) `its-iaas` gateway starts on port 8100
3. `lightspeed-service` starts on port 8080 (host-side, upstream unmodified)
4. For each of 11 scenarios per iteration:
   - Cleanup previous scenario
   - Create namespace + Docker Hub pull secret
   - Deploy broken workload (`setup.sh`)
   - `lightspeed-eval` sends query → OLS → (ITS gateway →) LLM → MCP tools → cluster
   - Judge scores response
   - Cleanup scenario
5. Print pass/fail summary
6. Kill all services

## Results

Results are stored at `eval_scenarios/results/traced_<label>/iter_XX/<tag>/`.

Each scenario produces:
- `evaluation_*_summary.json` — pass rates, token counts
- `evaluation_*_detailed.csv` — per-turn scores with judge reasoning

## Key Differences from Other Eval Setups

### vs. Eshwar's original (`ols-troubleshooting-eval` main branch)

| | Original | This branch |
|---|---|---|
| MCP server | npm `kubernetes-mcp-server` (read-only, port 8089) | Go `openshift-mcp-server` (read-write, port 8085) |
| Docker Hub | Direct pulls (rate limit risk) | Pull-through cache |
| config_drift setup | 80-iteration polling loop | `oc wait` + single log check |
| ITS support | None | `its_hub` IaaS gateway (no OLS patching) |

### vs. Shabana's (`its-openshift-mcp` branch `use_its_hub_v1`)

| | Shabana's | This branch |
|---|---|---|
| Scenarios | 3 (OOM, NetworkPolicy, Payments) | 11 |
| ITS integration | Patches OLS source code | IaaS gateway proxy (no patching) |
| OLS restarts for ITS | Required per budget change | Not needed (gateway handles it) |
| Judge model | gpt-4.1 | Configurable (`JUDGE_MODEL=gpt-4.1`) |

### OLS version note

This eval uses the latest upstream `lightspeed-service`. Recent versions (after commit `326e7620`, 2026-04-23) include the **tool output offloading** feature (OLS-2277), which intercepts large tool outputs and replaces them with search/read references. This changes the model's interaction pattern compared to older OLS versions. The `search_offloaded_content` tool may intermittently fail with `signal only works in main thread` — OLS handles this gracefully (logs error, model works around it) but it can affect accuracy non-deterministically.

Results from this eval are not directly comparable to runs using older OLS versions without offloading. To compare, either pin OLS to a pre-offloading commit or rerun older evals on the current OLS.

## After `crc delete`

Just run `make env-up` again. The pull-through cache persists on the host — CRC gets recreated, mirror reconfigured, and login refreshed automatically. No images need to be re-pulled from Docker Hub.
