#!/usr/bin/env bash
# Online GRPO training with verl for OLS troubleshooting
# Based on verl's gsm8k_multiturn example adapted for 4x GPU
#
# Changes from original:
#   - BASE_MODEL is parameterized (default: SFT warmup checkpoint, not base Qwen3-4B)
#     The base model doesn't emit <tool_call> tags, so verl's ToolAgentLoop never
#     invokes tools. SFT warmup teaches Hermes tool-calling syntax first.
#   - KL penalty increased 0.001 → 0.005 to prevent aggressive policy drift.
#     With a hackable LLM judge, stronger regularization keeps the policy closer
#     to the reference and reduces reward hacking (reward ↑ but eval ↓).
#   - Pre-training verification step checks that the model generates <tool_call>
#     tags before launching expensive GRPO training.
set -euo pipefail

cd "$(dirname "$0")"

ulimit -n 65535

# --- Configurable base model ---
# Default to SFT warmup checkpoint. Override with:
#   BASE_MODEL=Qwen/Qwen3-4B ./verl_train.sh
BASE_MODEL="${BASE_MODEL:-checkpoints/sft-warmup-merged}"

# Generate tool config if not exists
if [ ! -f verl_tools/tool_config.yaml ]; then
    python verl_tools/generate_tool_config.py
fi

# Generate dataset if not exists
if [ ! -f ols_train.parquet ]; then
    python verl_tools/generate_dataset.py --repeats 50
fi

# --- Pre-training verification ---
# Verify the model generates <tool_call> tags before expensive GRPO training.
# If this fails, the SFT warmup step is needed first.
echo "=== Pre-training tool execution verification ==="
echo "Model: ${BASE_MODEL}"
if python verl_tools/verify_tool_execution.py --model "${BASE_MODEL}" --samples 3; then
    echo "=== Verification passed — proceeding to GRPO training ==="
else
    echo "=== VERIFICATION FAILED ==="
    echo "The model does not generate <tool_call> tags."
    echo "Run SFT warmup first:"
    echo "  python build_sft_dataset.py --from-eval --troubleshooter-model gpt-5-mini"
    echo "  python sft/sft_warmup.py --data sft_dataset.jsonl --merge-and-save checkpoints/sft-warmup-merged"
    exit 1
fi

CONFIG_PATH="$(pwd)/verl_tools"

python3 -m verl.trainer.main_ppo \
    algorithm.adv_estimator=grpo \
    data.train_files=ols_train.parquet \
    data.train_batch_size=44 \
    data.max_prompt_length=2048 \
    data.max_response_length=4096 \
    data.filter_overlong_prompts=True \
    data.truncation=error \
    data.return_raw_chat=True \
    actor_rollout_ref.model.path="${BASE_MODEL}" \
    actor_rollout_ref.hybrid_engine=True \
    actor_rollout_ref.model.use_remove_padding=True \
    actor_rollout_ref.model.enable_gradient_checkpointing=True \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.ppo_mini_batch_size=44 \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=11 \
    actor_rollout_ref.actor.use_kl_loss=True \
    actor_rollout_ref.actor.kl_loss_coef=0.005 \
    actor_rollout_ref.actor.kl_loss_type=low_var_kl \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.actor.fsdp_config.param_offload=False \
    actor_rollout_ref.actor.fsdp_config.optimizer_offload=False \
    actor_rollout_ref.rollout.name=sglang \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.5 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
    actor_rollout_ref.rollout.n=4 \
    actor_rollout_ref.rollout.temperature=0.7 \
    actor_rollout_ref.rollout.mode=async \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=11 \
    actor_rollout_ref.rollout.multi_turn.enable=True \
    actor_rollout_ref.rollout.multi_turn.tool_config_path=verl_tools/tool_config.yaml \
    actor_rollout_ref.rollout.multi_turn.max_assistant_turns=15 \
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=11 \
    actor_rollout_ref.ref.fsdp_config.param_offload=True \
    algorithm.use_kl_in_reward=False \
    reward.custom_reward_function.path=verl_tools/ols_reward.py \
    reward.custom_reward_function.name=compute_score \
    trainer.critic_warmup=0 \
    trainer.logger='["console"]' \
    trainer.project_name=ols-grpo \
    trainer.experiment_name=qwen3-4b-grpo \
    trainer.n_gpus_per_node=4 \
    trainer.nnodes=1 \
    trainer.save_freq=5 \
    trainer.total_epochs=5 \
    "$@"
