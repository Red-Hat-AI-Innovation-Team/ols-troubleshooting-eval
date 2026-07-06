"""CPT with LoRA on Qwen3-8B using Megatron-Bridge.

Usage (inside NeMo container on 8x H100):
  torchrun --nproc_per_node=8 cpt_lora_qwen3_8b.py
  torchrun --nproc_per_node=8 cpt_lora_qwen3_8b.py --resume /data/checkpoints/qwen3_8b_cpt_lora

Config:
  - Model: Qwen3-8B (dense transformer, 8B params)
  - LoRA rank: 64, alpha: 64, targets: qkv/proj/fc
  - Sequence length: 4096
  - Data: pre-tokenized bin/idx from train_data__Qwen__Qwen3-8B.jsonl
"""
import argparse

from megatron.bridge.peft.lora import LoRA
from megatron.bridge.recipes.qwen import qwen3_8b_peft_config
from megatron.bridge.training.config import GPTDatasetConfig
from megatron.bridge.training.finetune import finetune
from megatron.bridge.training.gpt_step import forward_step


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help="Path to LoRA checkpoint dir to resume from (e.g. /data/checkpoints/qwen3_8b_cpt_lora)",
    )
    args, _ = parser.parse_known_args()

    # Start from PEFT config (TP=1, PP=1 for LoRA on single node)
    config = qwen3_8b_peft_config()

    # Load pretrained checkpoint
    config.checkpoint.pretrained_checkpoint = "/data/checkpoints/qwen3_8b_megatron"
    config.checkpoint.save = "/data/checkpoints/qwen3_8b_cpt_lora"
    config.checkpoint.save_interval = 10

    # Resume from LoRA checkpoint (loads adapter weights + optimizer + rng)
    if args.resume:
        config.checkpoint.load = args.resume

    # LoRA config: rank 64, standard transformer targets
    config.peft = LoRA(
        target_modules=["linear_qkv", "linear_proj", "linear_fc1", "linear_fc2"],
        dim=64,
        alpha=64,
        dropout=0.0,
    )

    # Parallelism: TP=2 (shards model across GPU pairs), PP=1
    config.model.tensor_model_parallel_size = 2
    config.model.pipeline_model_parallel_size = 1

    # Sequence length
    config.model.seq_length = 4096

    # Dataset — pre-tokenized bin/idx
    config.dataset = GPTDatasetConfig(
        random_seed=1234,
        sequence_length=4096,
        data_path=["/data/tokenized/cpt_qwen3_8b_preprocessed_text_document"],
        split="98,1,1",
        dataloader_type="single",
        num_workers=8,
        reset_position_ids=False,
        reset_attention_mask=False,
        eod_mask_loss=False,
        create_attention_mask=True,
        mmap_bin_files=False,
    )

    # Training config
    # ~5.6M tokens / (4096 tokens/seq * 256 global_batch) ≈ 5.3 steps/epoch
    # 10 epochs ≈ 55 iters
    config.train.train_iters = 55
    config.train.micro_batch_size = 2
    config.train.global_batch_size = 256

    # Optimizer — constant LR for CPT domain injection
    config.optimizer.lr = 3e-4
    config.optimizer.min_lr = 3e-4
    config.optimizer.adam_beta2 = 0.95

    # LR schedule — constant (no warmup, no decay)
    config.scheduler.lr_warmup_iters = 0
    config.scheduler.lr_decay_style = "constant"

    finetune(config=config, forward_step_func=forward_step)


if __name__ == "__main__":
    main()
