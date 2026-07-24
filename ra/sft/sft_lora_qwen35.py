"""SFT with LoRA on Qwen3.5-2B using Megatron-Bridge.

Usage (inside NeMo container on 8x H100):
  torchrun --nproc_per_node=8 sft_lora_qwen35.py

Config:
  - Model: Qwen3.5-2B (instruct baseline, same as CPT starting point)
  - LoRA rank: 64, alpha: 64, targets: qkv/proj/fc (text decoder only)
  - Sequence length: 65536 (64K context)
  - Parallelism: TP=2, CP=4, PP=1, DP=1, SP=True (8 GPUs)
  - GDN layers use all-to-all CP (hidden-parallel), attention uses ring CP
  - Data: pre-tokenized bin/idx from sft_dataset.jsonl (2,750 convos, ~44M tokens)

Note: Uses the VL PEFT recipe for correct Qwen3.5-2B model architecture.
Dataset swapped to GPTDatasetConfig for pre-tokenized bin/idx data.
"""
from megatron.bridge.peft.lora import LoRA
from megatron.bridge.recipes.qwen_vl.qwen35_vl import qwen35_vl_2b_peft_config
from megatron.bridge.training.config import GPTDatasetConfig
from megatron.bridge.training.finetune import finetune
from megatron.bridge.training.gpt_step import forward_step


def main():
    # Start from VL PEFT config (correct model arch for Qwen3.5-2B)
    config = qwen35_vl_2b_peft_config()

    # Load pretrained checkpoint (instruct-tuned baseline)
    config.checkpoint.pretrained_checkpoint = "/data/checkpoints/qwen35_2b_megatron"
    config.checkpoint.save = "/data/checkpoints/qwen35_sft"
    config.checkpoint.save_interval = 500

    # LoRA config: rank 64, standard transformer targets
    config.peft = LoRA(
        target_modules=["linear_qkv", "linear_proj", "linear_fc1", "linear_fc2"],
        dim=64,
        alpha=64,
        dropout=0.0,
    )

    # Parallelism: TP=2, CP=4, SP=True for 64K context on 8 GPUs
    # TP=2: splits model params, enables sequence parallelism
    # CP=4: splits 64K → 16K/GPU; GDN uses all-to-all, attention uses ring
    # SP=True: distributes LayerNorm/Dropout activations across TP group
    config.model.tensor_model_parallel_size = 2
    config.model.pipeline_model_parallel_size = 1
    config.model.context_parallel_size = 4
    config.model.sequence_parallel = True

    # Disable MTP (multi-token prediction) — incompatible with CP, not needed for SFT
    config.model.mtp_num_layers = 0

    # Selective activation recomputation — recompute attention internals
    config.model.recompute_granularity = "selective"
    config.model.recompute_modules = ["core_attn"]

    # VL recipe sets per-token loss; must disable average_in_collective for DDP compat
    config.ddp.average_in_collective = False

    # Sequence length: 64K
    config.model.seq_length = 65536

    # Dataset — pre-tokenized bin/idx from sft_dataset.jsonl
    config.dataset = GPTDatasetConfig(
        random_seed=1234,
        sequence_length=65536,
        data_path=["/data/sft_qwen35_preprocessed_text_document_text_document"],
        split="98,1,1",
        dataloader_type="single",
        num_workers=1,
        reset_position_ids=False,
        reset_attention_mask=False,
        eod_mask_loss=False,
        create_attention_mask=True,
        mmap_bin_files=True,
    )

    # Training config
    # 44M tokens / 65536 seq_len ≈ 672 sequences
    # TP=2, CP=4, DP=1 → micro_batch=1, grad accum to global_batch
    # global_batch=8 → 8 * 65536 = 524K tokens/step
    # 44M / 524K ≈ 84 steps for 1 epoch
    config.train.train_iters = 84
    config.train.micro_batch_size = 1
    config.train.global_batch_size = 8

    # Optimizer — cosine decay for SFT
    config.optimizer.lr = 2e-5
    config.optimizer.min_lr = 2e-6

    # LR schedule — warmup then cosine decay
    config.scheduler.lr_warmup_iters = 5
    config.scheduler.lr_decay_style = "cosine"

    finetune(config=config, forward_step_func=forward_step)


if __name__ == "__main__":
    main()
