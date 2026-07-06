"""SFT training script for OLS troubleshooting agent.

Reads pre-formatted SFT JSONL (each line has a 'text' field with chat-template-
applied text from build_sft_dataset.py), and fine-tunes a model using
SFTTrainer + LoRA + bitsandbytes (4-bit quantization).

Based on the HuggingFace Agents Course training template.

Usage:
    uv run python train_ols_agent.py --dataset sft_dataset.jsonl
    uv run python train_ols_agent.py --dataset sft_dataset.jsonl --model Qwen/Qwen3-8B --epochs 3
"""

import argparse
import json
import sys

import torch
from datasets import Dataset
from peft import LoraConfig, TaskType
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    set_seed,
)
from trl import SFTConfig, SFTTrainer


SEED = 42


def load_jsonl_dataset(path: str) -> Dataset:
    """Load JSONL file where each line has a 'text' field."""
    records: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            records.append({"text": record["text"]})
    return Dataset.from_list(records)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fine-tune a model for OLS troubleshooting with SFT + LoRA"
    )
    parser.add_argument(
        "--dataset", required=True,
        help="Path to SFT JSONL file (output of build_sft_dataset.py)",
    )
    parser.add_argument("--model", default="Qwen/Qwen3-8B", help="Base model name or path")
    parser.add_argument("--output-dir", default="ols-agent-sft", help="Output directory for checkpoints")
    parser.add_argument("--epochs", type=int, default=3, help="Number of training epochs")
    parser.add_argument("--batch-size", type=int, default=1, help="Per-device train batch size")
    parser.add_argument("--lr", type=float, default=1e-4, help="Learning rate")
    parser.add_argument("--lora-rank", type=int, default=16, help="LoRA rank dimension")
    parser.add_argument("--lora-alpha", type=int, default=64, help="LoRA alpha scaling factor")
    parser.add_argument("--max-seq-length", type=int, default=8192, help="Maximum sequence length")
    parser.add_argument("--gradient-accumulation-steps", type=int, default=4)
    parser.add_argument("--test-split", type=float, default=0.1, help="Fraction for test split")
    parser.add_argument("--push-to-hub", action="store_true", help="Push model to HuggingFace Hub")
    parser.add_argument("--hub-repo", type=str, default=None, help="Hub repo ID (user/model-name)")
    args = parser.parse_args()

    set_seed(SEED)

    # -----------------------------------------------------------------------
    # Load dataset
    # -----------------------------------------------------------------------
    print(f"Loading dataset from {args.dataset}...")
    dataset = load_jsonl_dataset(args.dataset)
    print(f"Loaded {len(dataset)} examples")

    if len(dataset) < 2:
        print("ERROR: Need at least 2 examples for train/test split", file=sys.stderr)
        sys.exit(1)

    dataset = dataset.train_test_split(test_size=args.test_split, seed=SEED)
    print(f"Train: {len(dataset['train'])}, Test: {len(dataset['test'])}")

    # -----------------------------------------------------------------------
    # Load tokenizer
    # -----------------------------------------------------------------------
    print(f"Loading tokenizer from {args.model}...")
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)

    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # -----------------------------------------------------------------------
    # Load model with 4-bit quantization
    # -----------------------------------------------------------------------
    print(f"Loading model {args.model} with 4-bit quantization...")
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )

    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        quantization_config=bnb_config,
        device_map="auto",
        trust_remote_code=True,
        attn_implementation="eager",
    )

    # -----------------------------------------------------------------------
    # LoRA configuration
    # -----------------------------------------------------------------------
    peft_config = LoraConfig(
        r=args.lora_rank,
        lora_alpha=args.lora_alpha,
        lora_dropout=0.05,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        task_type=TaskType.CAUSAL_LM,
    )

    # -----------------------------------------------------------------------
    # Training configuration
    # -----------------------------------------------------------------------
    training_args = SFTConfig(
        output_dir=args.output_dir,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.1,
        weight_decay=0.1,
        max_grad_norm=1.0,
        bf16=True,
        logging_steps=5,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to="tensorboard",
        push_to_hub=args.push_to_hub,
        hub_model_id=args.hub_repo,
        packing=True,
        max_seq_length=args.max_seq_length,
    )

    # -----------------------------------------------------------------------
    # Train
    # -----------------------------------------------------------------------
    print("Initializing SFTTrainer...")
    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset["train"],
        eval_dataset=dataset["test"],
        processing_class=tokenizer,
        peft_config=peft_config,
    )

    print("Starting training...")
    trainer.train()

    print(f"Saving model to {args.output_dir}...")
    trainer.save_model()
    tokenizer.save_pretrained(args.output_dir)

    if args.push_to_hub and args.hub_repo:
        print(f"Pushing to Hub: {args.hub_repo}...")
        trainer.push_to_hub(args.hub_repo)
        tokenizer.push_to_hub(args.hub_repo)

    print("Training complete.")


if __name__ == "__main__":
    main()
