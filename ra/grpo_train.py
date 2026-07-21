"""Offline GRPO training on scored rollouts.

Usage:
    uv run python grpo_train.py \
        --rollouts rollouts.jsonl \
        --model Qwen/Qwen3-4B \
        --output-dir grpo-qwen3-4b \
        --epochs 3

The training process:
1. Load rollouts (each has a conversation + reward score)
2. Group rollouts by scenario (same prompt)
3. Compute group-relative advantages: A_i = (r_i - mean(r_group)) / (std(r_group) + eps)
4. Convert conversations to token sequences using chat template
5. Create label masks: -100 for non-model tokens (system, user, tool results)
6. Train with advantage-weighted cross-entropy loss
"""

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import torch
from torch.utils.data import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

try:
    from transformers import BitsAndBytesConfig
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
except ImportError:
    pass  # Only needed for --lora mode


# ---------------------------------------------------------------------------
# Conversation conversion (matches build_sft_dataset.py format)
# ---------------------------------------------------------------------------

def convert_conversation(conversation: list[dict]) -> list[dict]:
    """Convert agent conversation (Message.model_dump() dicts) to chat format.

    Reuses the same logic as build_sft_dataset.py:convert_conversation so that
    the tokenizer's chat_template produces consistent results.
    """
    messages: list[dict] = []

    # Build tool_call_id -> tool_name map for tool result messages
    tc_id_to_name: dict[str, str] = {}
    for msg in conversation:
        for tc in msg.get("tool_calls", []):
            tc_id_to_name[tc["id"]] = tc["name"]

    for msg in conversation:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""

        if role == "user":
            messages.append({"role": "user", "content": content})

        elif role == "assistant":
            m: dict = {"role": "assistant"}
            if content:
                m["content"] = content
            if msg.get("tool_calls"):
                m["tool_calls"] = [
                    {
                        "type": "function",
                        "id": tc["id"],
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                    for tc in msg["tool_calls"]
                ]
            messages.append(m)

        elif role == "tool":
            for tr in msg.get("tool_results", []):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_call_id"],
                    "name": tc_id_to_name.get(tr["tool_call_id"], "unknown"),
                    "content": tr["content"],
                })

        elif role == "system":
            messages.append({"role": "system", "content": content})

    return messages


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

class GRPORolloutDataset(Dataset):
    """Dataset of tokenized rollouts with group-relative advantages.

    Each item is a dict with:
    - input_ids: token IDs for the full conversation
    - labels: token IDs (copy of input_ids; padding masked to -100 by collator)
    - attention_mask: 1 for real tokens
    - advantage: scalar advantage weight for this example
    """

    def __init__(
        self,
        rollouts_path: Path,
        tokenizer,
        max_seq_length: int = 16384,
    ):
        self.tokenizer = tokenizer
        self.max_seq_length = max_seq_length
        self.examples: list[dict] = []

        # Load rollouts
        raw_rollouts: list[dict] = []
        with open(rollouts_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    raw_rollouts.append(json.loads(line))

        # Group by scenario_id to compute advantages
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in raw_rollouts:
            groups[r["scenario_id"]].append(r)

        # Compute advantages per group
        for scenario_id, group in groups.items():
            rewards = [r["reward"] for r in group]
            mean_r = sum(rewards) / len(rewards)
            std_r = (sum((r - mean_r) ** 2 for r in rewards) / len(rewards)) ** 0.5

            for r in group:
                advantage = (r["reward"] - mean_r) / (std_r + 1e-8)

                messages = convert_conversation(r["conversation"])
                if not messages:
                    continue

                # Tokenize full conversation
                text = tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=False,
                )

                tokens = tokenizer(
                    text,
                    truncation=True,
                    max_length=max_seq_length,
                    return_tensors=None,
                )

                input_ids = tokens["input_ids"]

                # Build labels with masking: only train on assistant tokens.
                # Tokenize each message individually to find role boundaries.
                labels = [-100] * len(input_ids)
                pos = 0
                for msg in messages:
                    # Tokenize this single message to find its length
                    single_text = tokenizer.apply_chat_template(
                        messages[: messages.index(msg) + 1],
                        tokenize=False,
                        add_generation_prompt=False,
                    )
                    end_pos = len(tokenizer(
                        single_text,
                        truncation=True,
                        max_length=max_seq_length,
                        return_tensors=None,
                    )["input_ids"])

                    if msg["role"] == "assistant":
                        # Train on assistant tokens
                        for i in range(pos, min(end_pos, len(labels))):
                            labels[i] = input_ids[i]

                    pos = end_pos

                self.examples.append({
                    "input_ids": input_ids,
                    "labels": labels,
                    "attention_mask": tokens["attention_mask"],
                    "advantage": advantage,
                })

        print(f"Loaded {len(self.examples)} training examples from {len(groups)} scenario groups")

        if self.examples:
            advs = [e["advantage"] for e in self.examples]
            print(f"Advantages: min={min(advs):.2f}, max={max(advs):.2f}, mean={sum(advs)/len(advs):.2f}")

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        return self.examples[idx]


# ---------------------------------------------------------------------------
# Custom Trainer with advantage-weighted loss
# ---------------------------------------------------------------------------

class GRPOTrainer(Trainer):
    """Trainer that weights the cross-entropy loss by per-example advantages."""

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
        advantages = inputs.pop("advantage")

        outputs = model(
            input_ids=inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            labels=inputs["labels"],
        )

        # Weight the loss by the mean advantage in this batch.
        # Positive advantage upweights good rollouts, negative downweights bad ones.
        loss = outputs.loss * advantages.mean()

        return (loss, outputs) if return_outputs else loss


# ---------------------------------------------------------------------------
# Data collator
# ---------------------------------------------------------------------------

@dataclass
class GRPODataCollator:
    """Collator that pads input_ids, labels, attention_mask and stacks advantages."""

    tokenizer: object
    max_length: int = 16384

    def __call__(self, features):
        advantages = torch.tensor(
            [f["advantage"] for f in features], dtype=torch.float32,
        )

        # Remove advantage before passing to tokenizer.pad (it only handles tensor fields)
        pad_features = [{k: v for k, v in f.items() if k != "advantage"} for f in features]

        batch = self.tokenizer.pad(
            pad_features,
            padding=True,
            max_length=self.max_length,
            return_tensors="pt",
        )

        # Set padding tokens to -100 in labels so they're ignored in loss
        batch["labels"][batch["labels"] == self.tokenizer.pad_token_id] = -100

        batch["advantage"] = advantages
        return batch


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Offline GRPO training")
    parser.add_argument("--rollouts", type=Path, required=True, help="Path to rollouts.jsonl")
    parser.add_argument("--model", type=str, default="Qwen/Qwen3-4B", help="Base model")
    parser.add_argument("--output-dir", type=str, default="grpo-qwen3-4b")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=4)
    parser.add_argument("--max-seq-length", type=int, default=16384)
    parser.add_argument("--lora", action="store_true", help="Use QLoRA instead of full-param")
    parser.add_argument("--lora-rank", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=64)
    args = parser.parse_args()

    # Load tokenizer
    print(f"Loading tokenizer: {args.model}")
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if args.lora:
        # QLoRA: 4-bit quantized base + LoRA adapter
        print(f"Loading model: {args.model} (4-bit QLoRA)")
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
            attn_implementation="flash_attention_2",
            torch_dtype=torch.bfloat16,
        )

        model = prepare_model_for_kbit_training(model)

        lora_config = LoraConfig(
            r=args.lora_rank,
            lora_alpha=args.lora_alpha,
            lora_dropout=0.05,
            target_modules=[
                "q_proj", "k_proj", "v_proj", "o_proj",
                "gate_proj", "up_proj", "down_proj",
            ],
            task_type="CAUSAL_LM",
        )
        model = get_peft_model(model, lora_config)
        model.print_trainable_parameters()
    else:
        # Full parameter training
        print(f"Loading model: {args.model} (full-param bf16)")
        model = AutoModelForCausalLM.from_pretrained(
            args.model,
            device_map="auto",
            trust_remote_code=True,
            attn_implementation="flash_attention_2",
            torch_dtype=torch.bfloat16,
        )
        total = sum(p.numel() for p in model.parameters())
        trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
        print(f"trainable params: {trainable:,} || all params: {total:,} || trainable%: 100.0")

    # Load dataset
    print(f"Loading rollouts from {args.rollouts}")
    dataset = GRPORolloutDataset(
        rollouts_path=args.rollouts,
        tokenizer=tokenizer,
        max_seq_length=args.max_seq_length,
    )

    # Training args
    training_args = TrainingArguments(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.1,
        bf16=True,
        gradient_checkpointing=True,
        logging_steps=5,
        save_strategy="epoch",
        report_to="none",
        remove_unused_columns=False,  # keep 'advantage' column
    )

    # Train
    trainer = GRPOTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        data_collator=GRPODataCollator(
            tokenizer=tokenizer, max_length=args.max_seq_length,
        ),
    )

    print("Starting training...")
    trainer.train()

    # Save
    print(f"Saving to {args.output_dir}")
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    print("Done!")


if __name__ == "__main__":
    main()
