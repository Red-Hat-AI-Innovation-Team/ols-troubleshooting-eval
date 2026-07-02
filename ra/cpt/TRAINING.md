# CPT LoRA Training with Megatron-Bridge

How to run continued pretraining (CPT) with LoRA on Nemotron-3-Nano-30B-A3B using the NeMo container on 8x H100 GPUs.

Everything runs on `rh-h100-01` under `/mnt/nvme0n1/rawhad/ols-cpt/`.

## Prerequisites

- 8x H100 GPUs on `rh-h100-01`
- NeMo container: `nvcr.io/nvidia/nemo:26.06`
- Podman with NVIDIA CDI configured (already done on rh-h100-01)
- HuggingFace model weights downloaded
- Pre-tokenized dataset in Megatron bin/idx format

## End-to-end steps

### 1. Download model weights

```bash
ssh rh-h100-01
hf download nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-Base-BF16 \
  --local-dir /mnt/nvme0n1/rawhad/ols-cpt/nemotron-30b-base-hf
```

Use the **Base** model for CPT, not the instruct-tuned variant. ~59GB download.

### 2. Convert HF checkpoint to Megatron format

```bash
podman run --rm \
  --device nvidia.com/gpu=all \
  --ipc=host \
  -v /mnt/nvme0n1/rawhad/ols-cpt:/data:z \
  nvcr.io/nvidia/nemo:26.06 \
  python3 /opt/Megatron-Bridge/examples/conversion/convert_checkpoints.py import \
    --hf-model /data/nemotron-30b-base-hf \
    --megatron-path /data/checkpoints/nemotron_30b_megatron
```

Takes ~2 minutes. Produces a `iter_0000000/` directory with a single `.distcp` file (~59GB).

### 3. Tokenize the dataset

The training script expects Megatron's binary format (`.bin` + `.idx`), not raw JSONL. The input JSONL must have a `"text"` field per line.

```bash
podman run --rm \
  -v /mnt/nvme0n1/rawhad/ols-cpt:/data:z \
  -e RAYON_NUM_THREADS=1 \
  -e TOKENIZERS_PARALLELISM=false \
  nvcr.io/nvidia/nemo:26.06 \
  python3 /opt/Megatron-Bridge/3rdparty/Megatron-LM/tools/preprocess_data.py \
    --input /data/cpt_dataset.jsonl \
    --output-prefix /data/cpt_nemotron_preprocessed \
    --tokenizer-type HuggingFaceTokenizer \
    --tokenizer-model /data/nemotron-30b-base-hf \
    --workers 32 \
    --append-eod
```

Produces `cpt_nemotron_preprocessed_text_document.bin` (592MB) and `.idx` (3.6MB) from 185K documents in ~20 seconds.

**Important:** You must re-tokenize when switching models. Each model has a different vocabulary.

### 4. Launch training

```bash
podman run --rm \
  --device nvidia.com/gpu=all \
  --ipc=host \
  -e RAYON_NUM_THREADS=1 \
  -e TOKENIZERS_PARALLELISM=false \
  -e CUDA_DEVICE_MAX_CONNECTIONS=1 \
  -v /mnt/nvme0n1/rawhad/ols-cpt:/data:z \
  nvcr.io/nvidia/nemo:26.06 \
  torchrun --nproc_per_node=8 /data/cpt_lora_nemotron.py
```

For long runs, wrap in tmux:

```bash
tmux new-session -d -s nemotron-train '<podman command above> 2>&1 | tee /mnt/nvme0n1/rawhad/ols-cpt/train.log'
```

### 5. Monitor training

```bash
# Latest loss values
grep 'lm loss' /mnt/nvme0n1/rawhad/ols-cpt/train.log | tail -5

# Check for errors
grep -E 'Traceback|Error|FAILED' /mnt/nvme0n1/rawhad/ols-cpt/train.log | grep -v 'Triton\|nixl\|vllm'
```

### 6. Export LoRA checkpoint back to HuggingFace (after training)

```bash
podman run --rm \
  --device nvidia.com/gpu=all \
  --ipc=host \
  -v /mnt/nvme0n1/rawhad/ols-cpt:/data:z \
  nvcr.io/nvidia/nemo:26.06 \
  python3 /opt/Megatron-Bridge/examples/conversion/convert_checkpoints.py export \
    --hf-model /data/nemotron-30b-base-hf \
    --megatron-path /data/checkpoints/nemotron_30b_cpt_lora \
    --hf-path /data/nemotron-30b-cpt-lora-hf
```

## Podman flags explained

| Flag | Why |
|------|-----|
| `--device nvidia.com/gpu=all` | Expose all GPUs via NVIDIA CDI |
| `--ipc=host` | Shared memory for NCCL multi-GPU communication |
| `-e RAYON_NUM_THREADS=1` | Prevents rayon thread pool panic when 8 torchrun workers each init HF tokenizer |
| `-e TOKENIZERS_PARALLELISM=false` | Same — disables HF tokenizer parallelism inside each worker |
| `-e CUDA_DEVICE_MAX_CONNECTIONS=1` | Required by Megatron for correct overlap of compute and communication |
| `-v ...:/data:z` | Mount data directory; `:z` for SELinux relabeling on CentOS |

The `RAYON_NUM_THREADS` and `TOKENIZERS_PARALLELISM` flags are critical. Without them, you get a Rust panic from the tokenizer's thread pool when multiple torchrun workers initialize simultaneously.

## Training script walkthrough

The training script (`cpt_lora_nemotron.py`) does four things:

**1. Load the recipe** — `nemotron_3_nano_peft_config()` returns a pre-configured dataclass with the correct model architecture (Mamba2-Transformer Hybrid MoE), parallelism settings (EP=8 for expert parallelism), and LoRA defaults.

**2. Override the dataset** — The recipe defaults to `HFDatasetConfig` (downloads a HuggingFace dataset). For CPT with our own data, we swap it to `GPTDatasetConfig` pointing at the pre-tokenized bin/idx files.

**3. Set LoRA targets** — Six module types are adapted:
- `linear_qkv`, `linear_proj` — Transformer attention
- `linear_fc1`, `linear_fc2` — Transformer MLP / MoE expert FFN
- `in_proj`, `out_proj` — Mamba-2 state-space layers

This is important for Nemotron because it's a hybrid architecture. Standard Transformer-only LoRA would miss the Mamba layers entirely.

**4. Call `finetune()`** — This is Megatron-Bridge's entry point. Despite the name, it handles CPT too — the difference is just that we load a pretrained checkpoint and use unsupervised next-token prediction loss (via `forward_step` from `gpt_step`).

## Key hyperparameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| LoRA rank | 64 | Higher than default (32) for more capacity during domain injection |
| LoRA alpha | 64 | alpha=rank means scaling factor = 1.0 |
| LR | 3e-5 constant | No warmup, no decay. Conservative for CPT to avoid catastrophic forgetting |
| Batch size | micro=1, global=8 | 8 GPUs * 1 micro = 8 global. Small batches for domain data |
| Seq length | 4096 | 99% of our docs are under 6K tokens. Longer docs get chunked by dataloader |
| EP | 8 | Expert parallelism — one MoE expert shard per GPU |
| TP/PP | 1/1 | No tensor or pipeline parallelism needed for LoRA |
| Train iters | 4500 | ~1 epoch over 146M tokens |

## Adapting for a different model

To train a different model:

1. Find available recipes:
```bash
podman run --rm nvcr.io/nvidia/nemo:26.06 python3 -c "
import pkgutil, megatron.bridge.recipes as r
for _, name, _ in pkgutil.walk_packages(r.__path__, r.__name__ + '.'):
    print(name)
"
```

2. Inspect a recipe's defaults:
```bash
podman run --rm nvcr.io/nvidia/nemo:26.06 python3 -c "
from megatron.bridge.recipes.qwen.qwen3 import qwen3_8b_pretrain_config
c = qwen3_8b_pretrain_config()
print('seq_length:', c.model.seq_length)
print('dataset:', type(c.dataset).__name__)
print('peft:', c.peft)
"
```

3. Update the training script to import the correct recipe and adjust LoRA targets. Standard Transformer-only models use 4 targets (`linear_qkv`, `linear_proj`, `linear_fc1`, `linear_fc2`). Hybrid models with Mamba layers add `in_proj` and `out_proj`.

4. Re-tokenize the dataset with the new model's tokenizer.

5. Convert the new model's HF checkpoint to Megatron format.

## Observed training metrics (Nemotron-3-Nano-30B-A3B)

From the current run on 8x H100 80GB:

- ~2s/iteration (after first 10 iters of warmup at ~12s)
- Loss: started at ~1.18, stable
- Grad norm: 0.14-0.17 (healthy, no spikes)
- GPU memory: 35GB peak / 80GB available
- No NaN or skipped iterations
- ETA: ~2.5 hours for 4500 steps

## File layout on rh-h100-01

```
/mnt/nvme0n1/rawhad/ols-cpt/
  nemotron-30b-base-hf/              # HF weights (59GB)
  checkpoints/
    nemotron_30b_megatron/           # Converted Megatron checkpoint (59GB)
      iter_0000000/
        __0_0.distcp                 # Weights
        common.pt                    # Metadata
        tokenizer/                   # Tokenizer copy
    nemotron_30b_cpt_lora/           # LoRA checkpoints saved during training
  cpt_dataset.jsonl                  # Source dataset (185K docs, 573MB)
  cpt_nemotron_preprocessed_text_document.bin  # Tokenized (592MB)
  cpt_nemotron_preprocessed_text_document.idx  # Index (3.6MB)
  cpt_lora_nemotron.py               # Training script
  train.log                          # Training output
```
