# CPT Data Pipeline

Continued pretraining data for OLS troubleshooting. Two sources: domain documentation and Stack Overflow Q&A.

## Target Model

**Qwen3-8B** (`Qwen/Qwen3-8B`) via Megatron-Bridge with LoRA (rank 64), 4K context length.

Qwen3.5-9B (`Qwen/Qwen3.5-9B`) was initially considered but dropped — it uses a hybrid Gated DeltaNet + sparse MoE architecture (`8 × (3 × (Gated DeltaNet → FFN) → 1 × (Gated Attention → FFN))`), and preliminary research into Megatron-Bridge does not confirm training support for the DeltaNet attention variant. Qwen3-8B is a standard transformer with full recipe support (`qwen3_8b_peft_config`).

## Data Sources

### Documentation Repos (18 repos, ~28K files, ~70MB text)

All shallow-cloned into `repos/` on `rh-h100-01:~/rawhad/ols-cpt/repos/`.

| Repo | License | Content |
|------|---------|---------|
| openshift-docs | Apache-2.0 | OpenShift product docs (~10.3M tokens, 59% of docs corpus) |
| kubernetes-website | CC-BY-4.0 | Kubernetes docs (~3.7M tokens, 21%) |
| prometheus-docs | CC-BY-4.0 | Prometheus documentation |
| alertmanager | Apache-2.0 | Alertmanager docs |
| helm-www | Apache-2.0 | Helm documentation |
| istio.io | Apache-2.0 | Istio service mesh docs |
| argocd | Apache-2.0 | Argo CD GitOps docs |
| calico-docs | Apache-2.0 | Calico networking docs |
| cilium | Apache-2.0 | Cilium networking/security |
| coredns | Apache-2.0 | CoreDNS plugin docs |
| cert-manager | Apache-2.0 | cert-manager TLS automation |
| tektoncd/pipeline | Apache-2.0 | Tekton CI/CD pipelines |
| etcd-io/website | Apache-2.0 | etcd distributed KV store |
| cri-o | Apache-2.0 | CRI-O container runtime |
| kubernetes-sigs/nfs-subdir-external-provisioner | Apache-2.0 | NFS provisioner |
| ovn-kubernetes | Apache-2.0 | OVN-Kubernetes networking |
| kube-proxy | Apache-2.0 | kube-proxy docs (in k8s website) |
| containerd | Apache-2.0 | containerd runtime |

Grafana was excluded due to AGPL-3.0 license risk.

### Stack Overflow (60 SEDE queries, ~539MB CSVs, ~157K unique posts after dedup)

Queried via [SEDE](https://data.stackexchange.com/) (Stack Exchange Data Explorer). Licensed CC BY-SA.

**Query template**: Top-voted questions with accepted/highest-scored answers, returning Title, QuestionBody (HTML), AnswerBody (HTML), Score, QuestionId.

**Tags scraped**:

| Category | Tags |
|----------|------|
| Core K8s | kubernetes (3 date batches), kubectl, kubelet, kube-proxy |
| K8s resources | kubernetes-deployment, kubernetes-statefulset, kubernetes-pod, kubernetes-ingress, kubernetes-pvc, kubernetes-secrets, kubernetes-cronjob, kubernetes-jobs, kubernetes-hpa, kubernetes-rbac, kubernetes-dns, kubernetes-networking, kubernetes-health-check, kubernetes-security |
| OpenShift | openshift, openshift-4, operator-sdk, operator-lifecycle-manager |
| Containers | docker (4 date batches, score>=1 filter), containers, containerd, cri-o |
| Networking | istio, calico, coredns, nfs |
| CI/CD | argocd, tekton, helm |
| Monitoring | prometheus, promql, alertmanager, grafana |
| Security | cert-manager |
| Storage | etcd |
| Cross-domain | kubernetes+postgresql, kubernetes+redis, kubernetes+elasticsearch, kubernetes+mongodb, kubernetes+cassandra, kubernetes+mysql, kubernetes+java, kubernetes+grpc, kubernetes+nginx-ingress |

**Date splitting**: Large tags (kubernetes, docker) were split into date ranges to stay under SEDE's download size/timeout limits. Docker also required a `Score >= 1` filter for pre-2021 data.

**Scraping method**: Chrome Remote Debugging + Playwright CDP. Headless Playwright was blocked by Cloudflare on data.stackexchange.com, so we connected to an already-running Chrome instance via CDP. Exponential backoff with jitter (max 10 retries, max 60s wait) for flaky downloads.

## Processing Pipeline

### HTML-to-text conversion (`html_to_text.py`)

BeautifulSoup4-based converter optimized for CPT:
- Code blocks preserved with triple backticks + language hints from CSS classes
- Inline `<code>` preserved with backticks
- Headings converted to markdown `#` syntax
- Lists converted to `- ` bullets
- Bold/italic markup stripped (noise for pretraining)
- Images stripped
- Links reduced to text content only

Three strategies were compared (regex, HTMLParser, bs4) on sample data. bs4 chosen for best fidelity on code blocks and inline code.

### Dataset builder (`build_dataset.py`)

Produces `cpt_dataset.jsonl` (one JSON object per line):

```jsonl
{"text": "...", "source": "docs", "repo": "coredns", "path": "coredns/plugin.md"}
{"text": "...", "source": "stackoverflow", "tag": "kubernetes__2023_2027", "qid": "12345"}
```

- Doc files: reads `*.md`, `*.txt`, `*.adoc`, `*.rst` from `repos/`, skips files < 50 chars
- SO posts: reads all CSVs from `so_data/`, global dedup by QuestionId across all files, applies html_to_text, formats as `## Title\n\nquestion\n\n### Answer\n\nanswer`, skips posts < 100 chars

## Output Stats

| Metric | Value |
|--------|-------|
| Total documents | 185,631 |
| Doc files | 28,402 |
| SO posts (after dedup) | 157,229 |
| SO dupes removed | 33,311 (17% cross-tag overlap) |
| Dataset file size | 572.7 MB |
| Location | `rh-h100-01:~/rawhad/ols-cpt/cpt_dataset.jsonl` |

## Training

### Prerequisites

- Node with NVIDIA GPUs (tested on 8x H100 80GB)
- Podman with NVIDIA CDI configured (`/etc/cdi/nvidia.yaml`)
- NeMo container: `nvcr.io/nvidia/nemo:26.06`

Native install via `uv sync` does not work on CentOS Stream 9 (glibc 2.34) because `nvidia-resiliency-ext` requires glibc 2.39.

### Setup (one-time)

```bash
cd ~/rawhad/ols-cpt

# 1. Pull NeMo container (~17GB)
podman pull nvcr.io/nvidia/nemo:26.06

# 2. Clone Megatron-Bridge (needed for checkpoint conversion script)
git clone https://github.com/NVIDIA-NeMo/Megatron-Bridge.git megatron-bridge
cd megatron-bridge && git submodule update --init --recursive && cd ..

# 3. Download Qwen3-8B weights from HuggingFace
hf download Qwen/Qwen3-8B --local-dir ./qwen3-8b-hf

# 4. Convert HF checkpoint to Megatron format
podman run --rm --device nvidia.com/gpu=0 \
  -v ~/rawhad/ols-cpt:/data -w /opt/Megatron-Bridge \
  nvcr.io/nvidia/nemo:26.06 \
  python examples/conversion/convert_checkpoints.py import \
    --hf-model /data/qwen3-8b-hf \
    --megatron-path /data/checkpoints/qwen3_8b_megatron

# 5. Preprocess JSONL into Megatron bin/idx format
podman run --rm \
  -v ~/rawhad/ols-cpt:/data -w /opt/Megatron-Bridge \
  nvcr.io/nvidia/nemo:26.06 \
  python 3rdparty/Megatron-LM/tools/preprocess_data.py \
    --input /data/cpt_dataset.jsonl \
    --output-prefix /data/cpt_preprocessed \
    --tokenizer-type HuggingFaceTokenizer \
    --tokenizer-model /data/qwen3-8b-hf \
    --log-interval 10000 \
    --workers 32 \
    --append-eod
```

### Launch training

```bash
podman run --rm \
  --device nvidia.com/gpu=all \
  --ipc=host \
  -e RAYON_NUM_THREADS=1 \
  -e TOKENIZERS_PARALLELISM=false \
  -e CUDA_DEVICE_MAX_CONNECTIONS=1 \
  -v ~/rawhad/ols-cpt:/data \
  -w /data \
  nvcr.io/nvidia/nemo:26.06 \
  torchrun --nproc_per_node=8 cpt_lora_qwen3.py
```

### Training config (`cpt_lora_qwen3.py`)

| Parameter | Value |
|-----------|-------|
| Base model | Qwen3-8B (Megatron format) |
| Method | LoRA (rank 64, alpha 64) |
| Target modules | linear_qkv, linear_proj, linear_fc1, linear_fc2 |
| Sequence length | 4096 |
| Global batch size | 8 |
| Micro batch size | 1 |
| Learning rate | 2e-4 (cosine decay, 100-step warmup) |
| Train iterations | 5000 |
| Parallelism | TP=1, PP=1, DP=8 (8-way data parallel) |
| Checkpoint interval | every 500 steps |

### Corpus token stats

| Stat | Tokens |
|------|--------|
| P25 | 258 |
| Median | 441 |
| Mean | 785 |
| P75 | 783 |
| P99 | 5,982 |
| Max | 866,714 |
| Total | 145.9M |

75% of docs fit under 783 tokens. Docs longer than 4096 are chunked by the Megatron data loader.

### Observed performance (8x H100 80GB)

| Metric | Value |
|--------|-------|
| Step time | ~1.1s |
| GPU memory (peak) | ~48 GB per GPU |
| GPU TFLOP/s | ~187 per GPU |
| ETA (5000 iters) | ~1.5 hours |

## Troubleshooting

### `nvidia-resiliency-ext` wheel incompatibility

```
error: Distribution `nvidia-resiliency-ext` can't be installed because it doesn't have a wheel for the current platform
```

CentOS Stream 9 ships glibc 2.34; the wheel requires glibc 2.39. Use the NeMo container instead of native install.

### CDI device error: `cannot stat libEGL_nvidia.so`

```
Error: crun: cannot stat `/usr/lib64/libEGL_nvidia.so.575.57.08`: No such file or directory
```

The CDI config is stale (generated for an older driver version). Regenerate:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
```

### `--shm-size` conflict with `--ipc=host`

```
Error: invalid config provided: cannot set shmsize when running in the {host} IPC Namespace
```

Use `--ipc=host` without `--shm-size`. They are mutually exclusive — `--ipc=host` already gives full access to host shared memory.

### Rayon thread pool panic (multi-GPU only)

```
pyo3_runtime.PanicException: The global thread pool has not been initialized.:
ThreadPoolBuildError { kind: IOError(Os { code: 11, kind: WouldBlock,
message: "Resource temporarily unavailable" }) }
```

The HuggingFace tokenizer's Rust backend (rayon) fails to spawn threads when 8 processes initialize simultaneously. Fix by setting environment variables:

```bash
-e RAYON_NUM_THREADS=1 -e TOKENIZERS_PARALLELISM=false
```

### TP=4 assertion error (single GPU)

```
AssertionError: world size (1) is not divisible by total_model_size
(tensor_model_parallel_size=4 ...)
```

The pretrain recipe defaults to TP=4. Override in the training script:

```python
config.model.tensor_model_parallel_size = 1
config.model.pipeline_model_parallel_size = 1
```

## Data Location

All raw and processed data lives on `rh-h100-01:~/rawhad/ols-cpt/`:

```
ols-cpt/
  repos/                    # 18 shallow-cloned doc repos
  so_data/                  # 60 raw SEDE CSV files (539MB)
  so_corpus/                # intermediate plain text (from process_so_data.py)
  cpt_dataset.jsonl         # final CPT dataset (572.7MB)
  cpt_preprocessed_text_document.{bin,idx}  # tokenized for Megatron (577MB)
  qwen3-8b-hf/              # HuggingFace model weights
  checkpoints/qwen3_8b_megatron/           # converted Megatron checkpoint
  checkpoints/qwen3_8b_cpt_lora/           # training output (LoRA adapters)
  megatron-bridge/           # cloned Megatron-Bridge repo
  cpt_lora_qwen3.py         # training script
  html_to_text.py            # HTML converter
  build_dataset.py           # dataset builder
  count_tokens.py            # token counter for doc repos
```
