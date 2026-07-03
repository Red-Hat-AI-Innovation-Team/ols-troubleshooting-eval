"""Merge LoRA adapter into base model and save as full HF checkpoint."""
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

base_path = "/data/qwen3-8b-hf"
adapter_path = "/data/qwen3_8b_cpt_lora_adapter_hf"
output_path = "/data/qwen3_8b_cpt_merged_hf"

print("Loading base model...")
base = AutoModelForCausalLM.from_pretrained(base_path, torch_dtype="auto")

print("Loading adapter...")
model = PeftModel.from_pretrained(base, adapter_path)

print("Merging...")
merged = model.merge_and_unload()

print("Saving merged model...")
merged.save_pretrained(output_path)

print("Saving tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(base_path)
tokenizer.save_pretrained(output_path)

print("DONE")
