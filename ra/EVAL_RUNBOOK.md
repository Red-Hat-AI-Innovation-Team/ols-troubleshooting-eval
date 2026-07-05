# OLS Troubleshooting Eval Runbook

End-to-end guide for deploying a model on the H100 cluster and running the OLS troubleshooting evaluation against it.

## Architecture

```
rh-h100-XX (model node)                 rh-h100-01 (eval node)
┌──────────────────────┐                ┌────────────────────────────┐
│ vLLM server          │                │ CRC VM (OpenShift Local)   │
│ port 8000            │◄───────────────│   broken workloads         │
│ OpenAI-compatible    │  HTTP          │   11 scenarios             │
│                      │                │                            │
│ HF_HOME on NVMe      │                │ OLS (port 8080)            │
│ /mnt/nvme0n1/...     │                │   calls model via HTTP     │
└──────────────────────┘                │                            │
                                        │ openshift-mcp-server (8085)│
                                        │   K8s tools for OLS        │
                                        │                            │
                                        │ lightspeed-eval (judge)    │
                                        │   calls OpenAI gpt-5-mini  │
                                        │                            │
                                        │ Docker Hub cache (5000)    │
                                        └────────────────────────────┘
```

The model and eval run on **separate nodes**. The eval node needs CRC (OpenShift Local) for the broken workloads. Communication between nodes uses internal IPs (e.g., `10.241.128.21`).

---

## 1. Model Deployment (on any GPU node)

### Find the right vLLM deploy command

Always check the model's HuggingFace page first. Look for the "Use it with vLLM" section — it will list required flags like reasoning parsers, tool-call parsers, and context length.

Example: [nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16)

### Check GPU availability

```bash
ssh rh-h100-XX 'gpu status'
ssh rh-h100-XX 'nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader'
```

### Find a working vLLM venv

Nodes may have multiple venvs. The default one may be broken (ABI mismatch). Test before deploying:

```bash
# Check which venvs exist
ssh rh-h100-XX 'ls ~/rawhad/vllm_venv/bin/vllm ~/rawhad/vllm_venv_new/bin/vllm 2>/dev/null'

# Test the C extension loads
ssh rh-h100-XX '~/rawhad/vllm_venv_new/bin/python -c "import vllm._C; print(vllm.__version__)"'
```

On rh-h100-05, the working venv is `~/rawhad/vllm_venv_new` (v0.19.1). The default `vllm_venv` (v0.20.0) has a broken C extension.

### Download model-specific plugins

Some models require custom parser plugins. Download them before deploying:

```bash
# Nemotron-3-Nano requires a custom reasoning parser
ssh rh-h100-XX 'wget -O /mnt/nvme0n1/rawhad/nano_v3_reasoning_parser.py \
  https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16/resolve/main/nano_v3_reasoning_parser.py'
```

### Deploy

```bash
ssh rh-h100-XX "tmux new-session -d -s rohan-nemotron \
  'cd /mnt/nvme0n1/rawhad && \
   export HF_HOME=/mnt/nvme0n1/rawhad/hf_cache && \
   CUDA_VISIBLE_DEVICES=0,1 ~/rawhad/vllm_venv_new/bin/vllm serve \
     nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 \
     --served-model-name model \
     --max-num-seqs 8 \
     --tensor-parallel-size 2 \
     --max-model-len 131072 \
     --port 8000 \
     --trust-remote-code \
     --enable-auto-tool-choice \
     --tool-call-parser qwen3_coder \
     --reasoning-parser-plugin nano_v3_reasoning_parser.py \
     --reasoning-parser nano_v3 \
     2>&1 | tee /mnt/nvme0n1/rawhad/vllm_nemotron.log; sleep infinity'"
```

Key flags explained:

| Flag | Why |
|------|-----|
| `--served-model-name model` | Short alias used in API calls and eval config |
| `--max-model-len 131072` | 128K context. Must be >= OLS context_window_size (default 128000). **4096 is too small** — OLS prompts with MCP tools easily exceed it |
| `--tensor-parallel-size 2` | Nemotron is 30B params (~60GB BF16). 2x H100 gives headroom for KV cache |
| `--trust-remote-code` | Required for Mamba2-Transformer hybrid architecture |
| `--reasoning-parser nano_v3` | Separates `<think>` reasoning from final answer in API response |
| `--reasoning-parser-plugin` | Path to the custom parser file downloaded from HF |
| `--tool-call-parser qwen3_coder` | Enables tool calling (OLS uses MCP tools) |
| `--enable-auto-tool-choice` | Auto-detect tool calls in model output |
| `HF_HOME=/mnt/nvme0n1/...` | Use NVMe for model cache, not home directory |
| `; sleep infinity` | Keeps tmux session alive if vllm crashes, so you can read logs |

### Verify deployment

```bash
# Check server is up
ssh rh-h100-XX 'curl -s http://localhost:8000/v1/models | python3 -m json.tool'

# Smoke test
ssh rh-h100-XX "curl -s http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{\"model\": \"model\", \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}], \"max_tokens\": 100}' \
  | python3 -m json.tool"
```

Verify that the response has separate `reasoning` and `content` fields (not thinking text leaked into content).

### Tear down

```bash
ssh rh-h100-XX 'tmux kill-session -t rohan-nemotron'
```

---

## 2. Eval Node Setup (rh-h100-01)

### Prerequisites

All of these are already installed on rh-h100-01:

| Tool | Path | Version |
|------|------|---------|
| uv | `~/.local/bin/uv` | 0.11.21 |
| Go | `~/rawhad/bin/go/bin/go` | 1.24.4 |
| podman | `/home/linuxbrew/.linuxbrew/bin/podman` | 5.7.1 |
| oc | `~/.local/bin/oc` | (from crc) |
| crc | `~/rawhad/bin/crc` | 2.x |

### Project location

```
~/rawhad/ols-troubleshooting-eval/
```

The repo is on the `ra-sdg` branch.

### Required credential files

| File | Purpose |
|------|---------|
| `.openai_key` | OpenAI API key for the judge model (gpt-5-mini) |
| `.env` | `DOCKERHUB_USER` and `DOCKERHUB_TOKEN` for image cache |
| `~/.crc/pull-secret.json` | Red Hat pull secret for CRC |

### One-time setup (if not done)

```bash
cd ~/rawhad/ols-troubleshooting-eval
bash setup.sh      # clones lightspeed-service, installs lightspeed-eval CLI
make env-up        # starts CRC, Docker Hub cache, builds MCP server binary
```

### Find the model node's internal IP

Nodes don't resolve each other's hostnames. Use the internal IP:

```bash
ssh rh-h100-XX 'hostname -I'
# Example output: 10.241.128.21 10.7.0.16 ...
# Use the first IP (10.241.128.21)
```

Verify connectivity from the eval node:

```bash
ssh rh-h100-01 'curl -s http://10.241.128.21:8000/v1/models'
```

### Verify CRC is running

```bash
ssh rh-h100-01 'crc status'
```

Should show `CRC VM: Running`, `OpenShift: Running`.

### Verify oc login

```bash
ssh rh-h100-01 'oc whoami'
# Should print: kubeadmin

# If not:
ssh rh-h100-01 'KUBEADMIN_PASS=$(cat ~/.crc/machines/crc/kubeadmin-password) && \
  oc login -u kubeadmin -p "$KUBEADMIN_PASS" https://api.crc.testing:6443 --insecure-skip-tls-verify'
```

---

## 3. Running the Eval

### Command

```bash
ssh rh-h100-01

cd ~/rawhad/ols-troubleshooting-eval
export OPENAI_API_KEY=$(cat .openai_key)
export PATH=$HOME/rawhad/bin:$HOME/rawhad/bin/go/bin:$PATH

# Final eval (5 iterations)
bash run_eval.sh <label> http://<model_ip>:8000/v1 model <iterations>

# Example:
bash run_eval.sh nemotron-final http://10.241.128.21:8000/v1 model 5
```

### Running in tmux (recommended)

Long evals (~17 min per iteration) should run in tmux:

```bash
OPENAI_KEY=$(cat ~/rawhad/ols-troubleshooting-eval/.openai_key)

tmux new-session -d -s eval-run \
  "cd ~/rawhad/ols-troubleshooting-eval && \
   export OPENAI_API_KEY=$OPENAI_KEY && \
   export PATH=\$HOME/rawhad/bin:\$HOME/rawhad/bin/go/bin:\$PATH && \
   bash run_eval.sh nemotron-final http://10.241.128.21:8000/v1 model 5 \
   2>&1 | tee /mnt/nvme0n1/rawhad/nemotron_eval.log; sleep infinity"
```

**Important**: The `OPENAI_API_KEY` must be expanded at tmux creation time. Do NOT use `$ROPENAI_API_KEY` inside the tmux command string — tmux sessions don't inherit `.bashrc` env vars. Read the key from the file explicitly.

### Monitor progress

```bash
tail -f /mnt/nvme0n1/rawhad/nemotron_eval.log
# or
tmux attach -t eval-run
```

### View results

```bash
cd ~/rawhad/ols-troubleshooting-eval
./results.sh <label>
```

Results are in `eval_scenarios/results/traced_<label>/iter_XX/<scenario>/`.

---

## 4. Key Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_API_KEY` | (required) | API key for the judge model |
| `JUDGE_MODEL` | `gpt-5-mini` | Which LLM judges responses |
| `CONTEXT_WINDOW_SIZE` | `128000` | Must match model's `--max-model-len` |
| `TEMPERATURE` | (model default) | Sampling temperature |
| `ITS_BUDGET` | (unset) | Set to 4 or 8 to enable inference-time scaling |
| `ITS_ALGO` | `self-consistency` | ITS algorithm |
| `ITS_VOTE_MODE` | `tool_hierarchical` | ITS voting strategy |
| `MCP_EVALS` | (unset) | Set to `1` for MCP eval mode (needs Prometheus + payments demo) |

---

## 5. Scenarios

11 scenarios deployed as broken workloads on CRC:

| Scenario | Category | Difficulty |
|----------|----------|------------|
| envvar_missing | Resource Status | Easy |
| batch_failure | Log Analysis | Easy |
| storage_binding | Log Analysis | Easy |
| namespace_pod_count | Precision | Easy |
| readiness_probe_diagnosis | Resource Status | Easy |
| oom | Resource Status | Easy |
| ingress_rule_mismatch | Network Diagnosis | Medium |
| periodic_failure_window | Temporal Reasoning | Hard |
| scheduled_outage_detection | Temporal Reasoning | Hard |
| wrong_networkpolicy | Network Diagnosis | Medium (multi-turn, 3 turns) |
| config_drift_analysis | Config Analysis | Hard |

---

## 6. Troubleshooting

### CRC disk pressure — pods stuck in Pending

**Symptom**: Pods show `FailedScheduling` with `untolerated taint(s)`. Node has `node.kubernetes.io/disk-pressure` taint.

**Cause**: CRC VM disk is too small (default 31GB). OpenShift system images consume ~28GB, leaving < 15% free which triggers kubelet disk-pressure eviction.

**Fix**: Resize CRC disk:

```bash
crc stop
crc config set disk-size 100
crc start
```

After restart, re-login to the cluster:

```bash
KUBEADMIN_PASS=$(cat ~/.crc/machines/crc/kubeadmin-password)
oc login -u kubeadmin -p "$KUBEADMIN_PASS" https://api.crc.testing:6443 --insecure-skip-tls-verify
```

**Quick hack** (not recommended for final runs): Remove the taint in a loop:

```bash
while true; do oc adm taint nodes crc node.kubernetes.io/disk-pressure- 2>/dev/null; sleep 20; done
```

### ImagePullBackOff on wrong_networkpolicy

**Symptom**: `frontend` pod stuck in `ImagePullBackOff`.

**Cause**: The manifest uses `busybox` (no tag = `:latest`) but the Docker Hub cache only has `busybox:1.36`.

**Fix**: Pre-warm the cache:

```bash
podman pull docker.io/busybox:latest
podman tag docker.io/busybox:latest localhost:5000/library/busybox:latest
podman push localhost:5000/library/busybox:latest --tls-verify=false
```

Verify:

```bash
curl -s http://localhost:5000/v2/library/busybox/tags/list
# Should show: {"name":"library/busybox","tags":["1.36","latest"]}
```

### Model context too small — 400 error

**Symptom**: `This model's maximum context length is 4096 tokens. However, you requested 4096 output tokens and your prompt contains 28981 characters.`

**Cause**: vLLM `--max-model-len` is set too low. OLS system prompt + MCP tool definitions + conversation history easily exceeds 4096 tokens.

**Fix**: Redeploy the model with `--max-model-len 131072` (128K). This must be >= the eval's `CONTEXT_WINDOW_SIZE` (default 128000).

### OPENAI_API_KEY not found by judge

**Symptom**: `lightspeed_evaluation.core.system.exceptions.LLMError: OPENAI_API_KEY environment variable is required for OpenAI provider`

**Cause**: `OPENAI_API_KEY` not set in the shell running `run_eval.sh`. Common when using tmux — the env var from `.bashrc` isn't inherited.

**Fix**: Set it explicitly from the `.openai_key` file:

```bash
export OPENAI_API_KEY=$(cat .openai_key)
```

### oc login fails after CRC restart

**Symptom**: `error: Missing or incomplete configuration info` or `401 Unauthorized`.

**Cause**: CRC restart regenerates credentials. Old kubeconfig context is stale.

**Fix**: Re-login with the password from CRC's config:

```bash
KUBEADMIN_PASS=$(cat ~/.crc/machines/crc/kubeadmin-password)
oc login -u kubeadmin -p "$KUBEADMIN_PASS" https://api.crc.testing:6443 --insecure-skip-tls-verify
```

### vLLM crashes with ImportError on _C.abi3.so

**Symptom**: `ImportError: .../vllm/_C.abi3.so: undefined symbol: _ZN3c1013MessageLoggerC1ENS_14SourceLocationEib`

**Cause**: PyTorch ABI mismatch in the vllm venv. The venv was built against a different PyTorch version.

**Fix**: Use a different venv. Check which ones exist and test them:

```bash
ls ~/rawhad/vllm_venv*/bin/vllm
# Test each:
~/rawhad/vllm_venv_new/bin/python -c "import vllm._C; print('OK')"
```

### SSH into CRC VM

```bash
ssh -p 2222 -i ~/.crc/machines/crc/id_ed25519 core@127.0.0.1
```

Note: the key is `id_ed25519` (not `id_ecdsa`). Port is `2222` (not 22).

---

## 7. Variations

### Different models

Change the model ID and flags. Always check the HF model page for recommended vLLM flags.

| Model | TP | Context | Special Flags |
|-------|----|---------|---------------|
| Nemotron-3-Nano-30B-A3B | 2 | 131072 | `--reasoning-parser nano_v3 --reasoning-parser-plugin nano_v3_reasoning_parser.py --tool-call-parser qwen3_coder` |
| Qwen3.6 35B-A3B | 2 | 32768 | `--enable-auto-tool-choice --tool-call-parser qwen3_coder` |
| Gemma 4 12B-IT | 1 | 32768 | `--enable-auto-tool-choice --tool-call-parser gemma4` |
| Gemma 4 31B-IT | 2 | 32768 | `--enable-auto-tool-choice --tool-call-parser gemma4` |

The `--served-model-name model` and the model name arg to `run_eval.sh` must match.

### Inference-time scaling (ITS)

```bash
ITS_BUDGET=8 ITS_ALGO=self-consistency ITS_VOTE_MODE=tool_hierarchical \
  bash run_eval.sh <label>-its8 http://<ip>:8000/v1 model 5
```

This starts an ITS gateway on port 8100 that fans out N parallel calls to the model and votes on the best response.

### ITS sweep (multiple configs)

```bash
./run_its_sweep.sh http://<ip>:8000/v1 model 5
```

Runs all combinations of `ITS_BUDGET={4,8}` x `ITS_VOTE_MODE={tool_hierarchical,tool_flat_all}` plus a baseline.

### Using a different judge model

```bash
JUDGE_MODEL=claude-haiku-4-5 bash run_eval.sh <label> http://<ip>:8000/v1 model 5
```

---

## 8. Node Reference

### Internal IPs

Find any node's internal IP:

```bash
ssh rh-h100-XX 'hostname -I | awk "{print \$1}"'
```

Known IPs (may change):

| Node | Internal IP |
|------|-------------|
| rh-h100-05 | 10.241.128.21 |

### NVMe paths

All nodes have a 7TB NVMe at `/mnt/nvme0n1/`. Use it for:
- HF model cache: `/mnt/nvme0n1/rawhad/hf_cache`
- vLLM logs: `/mnt/nvme0n1/rawhad/vllm_*.log`
- Eval logs: `/mnt/nvme0n1/rawhad/*_eval*.log`
- CRC registry cache: `/mnt/nvme0n1/registry-cache`

### CRC VM specs (rh-h100-01)

| Setting | Value |
|---------|-------|
| Disk | 100GB (resized from 31GB default) |
| RAM | ~11GB |
| SSH | `ssh -p 2222 -i ~/.crc/machines/crc/id_ed25519 core@127.0.0.1` |
| OpenShift | v4.21.14 |

---

## 9. Checklist Before Final Eval

- [ ] Model deployed and responding on the model node
- [ ] Reasoning parser separates thinking from content (check API response)
- [ ] `--max-model-len` >= 128000
- [ ] CRC running, `oc whoami` returns `kubeadmin`
- [ ] No disk-pressure taint: `oc get nodes` shows `Ready` with no taints
- [ ] Docker Hub cache has all images including `busybox:latest`
- [ ] `.openai_key` exists and is valid
- [ ] `OPENAI_API_KEY` is set in the shell (not relying on `.bashrc`)
- [ ] Model node is reachable from eval node: `curl http://<ip>:8000/v1/models`
- [ ] Test run with 1 iteration passes without errors
