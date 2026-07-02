"""CPT with LoRA on Qwen3.5-2B using Megatron-Bridge.

Usage (inside NeMo container on 8x H100):
  torchrun --nproc_per_node=8 cpt_lora_qwen35.py

Config:
  - Model: Qwen3.5-2B (GDN+Attention hybrid, dense, 2B params)
  - LoRA rank: 64, alpha: 64, targets: qkv/proj/fc (text decoder only)
  - Sequence length: 4096
  - Data: pre-tokenized bin/idx from cpt_dataset.jsonl (185K docs, ~146M tokens)

Note: Qwen3.5-2B is a VL model, but we use only the text decoder for CPT.
The VL PEFT recipe provides the correct model architecture; we swap the
dataset to GPTDatasetConfig for text-only pre-tokenized data.
"""
from megatron.bridge.peft.lora import LoRA
from megatron.bridge.recipes.qwen_vl.qwen35_vl import qwen35_vl_2b_peft_config
from megatron.bridge.training.config import GPTDatasetConfig
from megatron.bridge.training.finetune import finetune
from megatron.bridge.training.gpt_step import forward_step


def main():
    # Start from VL PEFT config (correct model arch for Qwen3.5-2B)
    config = qwen35_vl_2b_peft_config()

    # Load pretrained checkpoint (this makes it CPT, not random-init)
    config.checkpoint.pretrained_checkpoint = "/data/checkpoints/qwen35_2b_megatron"
    config.checkpoint.save = "/data/checkpoints/qwen35_2b_cpt_lora"
    config.checkpoint.save_interval = 500

    # LoRA config: rank 64, standard transformer targets
    config.peft = LoRA(
        target_modules=["linear_qkv", "linear_proj", "linear_fc1", "linear_fc2"],
        dim=64,
        alpha=64,
        dropout=0.0,
    )

    # Parallelism: TP=1, PP=1 (2B model fits on single GPU easily)
    config.model.tensor_model_parallel_size = 1
    config.model.pipeline_model_parallel_size = 1

    # Sequence length
    config.model.seq_length = 4096

    # Dataset — swap from VL HFDatasetConversationProvider to GPTDatasetConfig (bin/idx)
    config.dataset = GPTDatasetConfig(
        random_seed=1234,
        sequence_length=4096,
        data_path=["/data/cpt_qwen35_preprocessed_text_document"],
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
    # ~146M tokens / (4096 tokens/seq * 256 global_batch) ≈ 140 steps for 1 epoch
    # 1M tokens/step (256 * 4096 = 1,048,576)
    config.train.train_iters = 140
    config.train.micro_batch_size = 2
    config.train.global_batch_size = 256

    # Optimizer — constant LR for CPT domain injection
    config.optimizer.lr = 3e-5
    config.optimizer.min_lr = 3e-5  # constant LR, no decay

    # LR schedule — constant (no warmup, no decay)
    config.scheduler.lr_warmup_iters = 0
    config.scheduler.lr_decay_style = "constant"

    finetune(config=config, forward_step_func=forward_step)


if __name__ == "__main__":
    main()
