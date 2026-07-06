from megatron.bridge import AutoBridge

bridge = AutoBridge.from_hf_pretrained("/data/qwen3-8b-hf")
bridge.export_adapter_ckpt(
    peft_checkpoint="/data/checkpoints/qwen3_8b_cpt_lora/iter_0000055",
    output_path="/data/qwen3_8b_cpt_lora_adapter_hf",
)
print("DONE")
