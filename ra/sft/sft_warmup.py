"""SFT warmup: LoRA fine-tune Qwen3-4B on tool-calling traces.

Trains the base model to reliably emit Hermes-style <tool_call> tags before
GRPO training begins. Without this warmup, the base model doesn't generate
tool calls and verl's ToolAgentLoop never invokes the PostgreSQL-backed tools.

Input: JSONL file from build_sft_dataset.py (--from-eval mode), where each
line has a "text" field containing a fully-formatted chat template string
with tool-calling traces.

Usage:
    # Basic training
    uv run python sft/sft_warmup.py --data sft_dataset.jsonl

    # Custom config
    uv run python sft/sft_warmup.py \
        --data sft_dataset.jsonl \
        --base-model Qwen/Qwen3-4B \
        --epochs 5 \
        --lora-rank 16 \
        --output-dir checkpoints/sft-warmup

    # Merge LoRA into base model for verl
    uv run python sft/sft_warmup.py \
        --data sft_dataset.jsonl \
        --merge-and-save checkpoints/sft-warmup-merged
"""

import argparse
import json
from pathlib import Path

import torch
from datasets import Dataset
from peft import LoraConfig, get_peft_model, TaskType
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    TrainingArguments,
    Trainer,
    DataCollatorForLanguageModeling,
)


def load_sft_jsonl(path: str) -> list[str]:
    """Load pre-formatted text from SFT JSONL (output of build_sft_dataset.py)."""
    texts = []
    with open(path) as f:
        for line in f:
            record = json.loads(line)
            texts.append(record["text"])
    return texts


def tokenize_texts(texts: list[str], tokenizer, max_length: int) -> Dataset:
    """Tokenize pre-formatted texts into a HuggingFace Dataset."""
    all_input_ids = []
    all_attention_mask = []
    all_labels = []

    for text in texts:
        encoded = tokenizer(
            text,
            truncation=True,
            max_length=max_length,
            padding=False,
            return_tensors=None,
        )
        input_ids = encoded["input_ids"]
        attention_mask = encoded["attention_mask"]
        # Labels = input_ids (standard causal LM training)
        labels = input_ids.copy()

        all_input_ids.append(input_ids)
        all_attention_mask.append(attention_mask)
        all_labels.append(labels)

    return Dataset.from_dict({
        "input_ids": all_input_ids,
        "attention_mask": all_attention_mask,
        "labels": all_labels,
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="SFT warmup for Qwen3-4B tool calling")
    parser.add_argument("--data", required=True, help="Path to SFT JSONL from build_sft_dataset.py")
    parser.add_argument("--base-model", default="Qwen/Qwen3-4B", help="Base model to fine-tune")
    parser.add_argument("--output-dir", default="checkpoints/sft-warmup", help="Output directory for adapter")
    parser.add_argument("--merge-and-save", default=None,
                        help="If set, merge LoRA into base model and save to this path")
    parser.add_argument("--epochs", type=int, default=3, help="Number of training epochs (3-5 recommended)")
    parser.add_argument("--lora-rank", type=int, default=16, help="LoRA rank")
    parser.add_argument("--lora-alpha", type=int, default=32, help="LoRA alpha (typically 2x rank)")
    parser.add_argument("--lr", type=float, default=2e-4, help="Learning rate")
    parser.add_argument("--batch-size", type=int, default=1, help="Per-device batch size")
    parser.add_argument("--grad-accum", type=int, default=8, help="Gradient accumulation steps")
    parser.add_argument("--max-length", type=int, default=8192, help="Max sequence length")
    parser.add_argument("--bf16", action="store_true", default=True, help="Use bfloat16 (default: True)")
    args = parser.parse_args()

    print(f"Loading tokenizer: {args.base_model}")
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    print(f"Loading SFT data: {args.data}")
    texts = load_sft_jsonl(args.data)
    print(f"  {len(texts)} traces loaded")

    if len(texts) == 0:
        print("ERROR: No training data. Generate traces first with build_sft_dataset.py --from-eval")
        return

    print("Tokenizing...")
    dataset = tokenize_texts(texts, tokenizer, args.max_length)
    print(f"  {len(dataset)} examples, avg length: {sum(len(x) for x in dataset['input_ids']) / len(dataset):.0f} tokens")

    print(f"Loading base model: {args.base_model}")
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        torch_dtype=torch.bfloat16 if args.bf16 else torch.float32,
        trust_remote_code=True,
        attn_implementation="flash_attention_2",
    )

    # LoRA config targeting attention projections
    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=args.lora_rank,
        lora_alpha=args.lora_alpha,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        bias="none",
    )

    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    training_args = TrainingArguments(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.1,
        bf16=args.bf16,
        logging_steps=1,
        save_strategy="epoch",
        save_total_limit=2,
        remove_unused_columns=False,
        dataloader_pin_memory=True,
        report_to="none",
        gradient_checkpointing=True,
    )

    data_collator = DataCollatorForLanguageModeling(
        tokenizer=tokenizer,
        mlm=False,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        data_collator=data_collator,
    )

    print(f"\nStarting SFT warmup training:")
    print(f"  Epochs: {args.epochs}")
    print(f"  LoRA rank: {args.lora_rank}, alpha: {args.lora_alpha}")
    print(f"  Targets: q_proj, k_proj, v_proj, o_proj")
    print(f"  LR: {args.lr}, batch: {args.batch_size} × {args.grad_accum} accum")
    print(f"  Max length: {args.max_length}")

    trainer.train()

    # Save adapter
    model.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    print(f"\nAdapter saved to {args.output_dir}")

    # Optionally merge LoRA into base model
    if args.merge_and_save:
        print(f"\nMerging LoRA into base model...")
        merged_model = model.merge_and_unload()
        merged_model.save_pretrained(args.merge_and_save)
        tokenizer.save_pretrained(args.merge_and_save)
        print(f"Merged model saved to {args.merge_and_save}")


if __name__ == "__main__":
    main()
