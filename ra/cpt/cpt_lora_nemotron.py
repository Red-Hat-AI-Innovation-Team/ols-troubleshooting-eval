"""CPT with LoRA on Nemotron-3-Nano-30B-A3B using Megatron-Bridge.

Usage (inside NeMo container on 8x H100):
  torchrun --nproc_per_node=8 cpt_lora_nemotron.py

Config:
  - Model: Nemotron-3-Nano-30B-A3B (Mamba2-Transformer Hybrid MoE, 30B total / 3.5B active)
  - LoRA rank: 64, alpha: 64, targets: qkv/proj/fc + Mamba in_proj/out_proj
  - Sequence length: 4096
  - Expert parallelism: EP=8 (one expert shard per GPU)
  - Data: pre-tokenized bin/idx from cpt_dataset.jsonl (185K docs, ~146M tokens)
"""
from megatron.bridge.peft.lora import LoRA
from megatron.bridge.recipes.nemotronh.nemotron_3_nano import nemotron_3_nano_peft_config
from megatron.bridge.training.config import GPTDatasetConfig
from megatron.bridge.training.finetune import finetune
from megatron.bridge.training.gpt_step import forward_step


def main():
    # Start from PEFT config (has LoRA defaults + EP=8 + correct model arch)
    config = nemotron_3_nano_peft_config()

    # Load pretrained checkpoint (this makes it CPT, not random-init)
    config.checkpoint.pretrained_checkpoint = "/data/checkpoints/nemotron_30b_megatron"
    config.checkpoint.save = "/data/checkpoints/nemotron_30b_cpt_lora"
    config.checkpoint.save_interval = 500

    # LoRA config: rank 64, includes Mamba-2 layers (in_proj, out_proj)
    config.peft = LoRA(
        target_modules=[
            "linear_qkv", "linear_proj",   # Transformer attention
            "linear_fc1", "linear_fc2",     # Transformer MLP / MoE experts
            "in_proj", "out_proj",          # Mamba-2 layers
        ],
        dim=64,
        alpha=64,
        dropout=0.0,
    )

    # Parallelism: TP=1, PP=1, EP=8 (from recipe default)
    config.model.tensor_model_parallel_size = 1
    config.model.pipeline_model_parallel_size = 1
    # EP=8 already set by recipe

    # Sequence length
    config.model.seq_length = 4096

    # Dataset — swap from HFDatasetConfig to GPTDatasetConfig (bin/idx)
    config.dataset = GPTDatasetConfig(
        random_seed=1234,
        sequence_length=4096,
        data_path=["/data/cpt_nemotron_preprocessed_text_document"],
        split="9998,1,1",
        dataloader_type="single",
        num_workers=8,
        reset_position_ids=False,
        reset_attention_mask=False,
        eod_mask_loss=False,
        create_attention_mask=True,
        mmap_bin_files=False,
    )

    # Training config
    # ~146M tokens / (4096 tokens/seq * 8 global_batch) ≈ 4,456 steps for 1 epoch
    config.train.train_iters = 140
    config.train.micro_batch_size = 2
    config.train.global_batch_size = 256

    # Optimizer — constant LR for CPT domain injection
    config.optimizer.lr = 3e-4
    config.optimizer.min_lr = 3e-4  # constant LR, no decay

    # LR schedule — constant (no warmup, no decay)
    config.scheduler.lr_warmup_iters = 0
    config.scheduler.lr_decay_style = "constant"

    finetune(config=config, forward_step_func=forward_step)


if __name__ == "__main__":
    main()
