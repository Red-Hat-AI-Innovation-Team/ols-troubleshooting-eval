#!/usr/bin/env bash
# Online GRPO training with verl for OLS troubleshooting
# Based on verl's gsm8k_multiturn example adapted for 4x GPU
#
# Key settings (learned from v1-v7 experiments):
#   - DAPO (filter_groups + DAPORewardManager) instead of vanilla GRPO
#   - enable_thinking=False: Qwen3's thinking mode burns 85% of response tokens
#     on <think> blocks during training, but eval via vLLM strips them. Disabling
#     thinking aligns training behavior with eval inference.
#   - rollout.name=vllm (not sglang — sglang not installed)
#   - filter_overlong_prompts=False (tool schemas make prompts ~8500 tokens)
#   - optimizer_offload=True + gpu_memory_utilization=0.2 to avoid OOM
#   - KL penalty 0.005 to prevent reward hacking (reward ↑ but eval ↓)
set -euo pipefail

cd "$(dirname "$0")"

ulimit -n 65535

# Ray 2.56 uv hook breaks verl (creates isolated venv missing ray itself)
export RAY_ENABLE_UV_RUN_RUNTIME_ENV=0

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

# Observability: check tool execution during training
# grep '<tool_call>' /tmp/ray/session_latest/logs/worker-*.out
# grep 'execute() tool=' /tmp/ray/session_latest/logs/worker-*.out

python3 -m verl.trainer.main_ppo \
    algorithm.adv_estimator=grpo \
    data.train_files=ols_train.parquet \
    data.train_batch_size=16 \
    data.max_prompt_length=2048 \
    data.max_response_length=4096 \
    data.filter_overlong_prompts=False \
    data.truncation=error \
    data.return_raw_chat=True \
    +data.apply_chat_template_kwargs.enable_thinking=False \
    +algorithm.filter_groups.enable=True \
    +algorithm.filter_groups.metric=acc \
    reward.reward_manager=DAPORewardManager \
    actor_rollout_ref.model.path="${BASE_MODEL}" \
    actor_rollout_ref.hybrid_engine=True \
    actor_rollout_ref.model.use_remove_padding=True \
    actor_rollout_ref.model.enable_gradient_checkpointing=True \
    actor_rollout_ref.actor.optim.lr=1e-6 \
    actor_rollout_ref.actor.ppo_mini_batch_size=16 \
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=4 \
    actor_rollout_ref.actor.use_kl_loss=True \
    actor_rollout_ref.actor.kl_loss_coef=0.005 \
    actor_rollout_ref.actor.kl_loss_type=low_var_kl \
    actor_rollout_ref.actor.entropy_coeff=0 \
    actor_rollout_ref.actor.fsdp_config.param_offload=False \
    actor_rollout_ref.actor.fsdp_config.optimizer_offload=True \
    actor_rollout_ref.rollout.name=vllm \
    actor_rollout_ref.rollout.gpu_memory_utilization=0.2 \
    actor_rollout_ref.rollout.tensor_model_parallel_size=1 \
    actor_rollout_ref.rollout.n=2 \
    actor_rollout_ref.rollout.temperature=0.7 \
    actor_rollout_ref.rollout.mode=async \
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=4 \
    actor_rollout_ref.rollout.multi_turn.enable=True \
    actor_rollout_ref.rollout.multi_turn.tool_config_path=verl_tools/tool_config.yaml \
    actor_rollout_ref.rollout.multi_turn.max_assistant_turns=15 \
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=4 \
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
    trainer.rollout_data_dir=./rollout_dumps \
    "$@"
