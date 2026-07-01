"""CPT with LoRA on Qwen3-8B using Megatron-Bridge.

Usage (inside NeMo container):
  torchrun --nproc_per_node=1 cpt_lora_qwen3.py

Config:
  - Model: Qwen3-8B
  - LoRA rank: 64, alpha: 64
  - Sequence length: 4096
  - Data: pre-tokenized bin/idx from cpt_dataset.jsonl
"""
from megatron.bridge.peft.lora import LoRA
from megatron.bridge.recipes.qwen import qwen3_8b_pretrain_config
from megatron.bridge.training.finetune import finetune
from megatron.bridge.training.gpt_step import forward_step


def main():
    # Start from pretrain config (uses GPTDatasetConfig with bin/idx support)
    config = qwen3_8b_pretrain_config()

    # Load pretrained checkpoint (this makes it CPT, not random-init)
    config.checkpoint.pretrained_checkpoint = "/data/checkpoints/qwen3_8b_megatron"
    config.checkpoint.save = "/data/checkpoints/qwen3_8b_cpt_lora"
    config.checkpoint.save_interval = 500

    # LoRA config: rank 64
    config.peft = LoRA(
        target_modules=["linear_qkv", "linear_proj", "linear_fc1", "linear_fc2"],
        dim=64,
        alpha=64,
        dropout=0.0,
    )

    # Parallelism: TP=1 for LoRA on single GPU
    config.model.tensor_model_parallel_size = 1
    config.model.pipeline_model_parallel_size = 1

    # Sequence length
    config.model.seq_length = 4096
    config.dataset.sequence_length = 4096

    # Dataset — bin/idx preprocessed data
    config.dataset.data_path = ["/data/cpt_preprocessed_text_document"]

    # Training config
    config.train.train_iters = 5000
    config.train.micro_batch_size = 1
    config.train.global_batch_size = 8

    # Optimizer (beta2=0.95 per Qwen3 technical reports)
    config.optimizer.lr = 3e-5
    config.optimizer.adam_beta2 = 0.95
    config.optimizer.min_lr = 3e-5  # constant LR, no decay

    # LR schedule — constant (no decay)
    config.scheduler.lr_warmup_iters = 0
    config.scheduler.lr_decay_style = "constant"

    finetune(config=config, forward_step_func=forward_step)


if __name__ == "__main__":
    main()
