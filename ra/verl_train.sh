#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Generate tool config if not exists
if [ ! -f verl_tools/tool_config.yaml ]; then
    python verl_tools/generate_tool_config.py
fi

# Generate dataset if not exists
if [ ! -f ols_train.parquet ]; then
    python verl_tools/generate_dataset.py --repeats 50
fi

python3 -m verl.trainer.main_ppo \
    algorithm.adv_estimator=grpo \
    data.train_files=ols_train.parquet \
    data.max_prompt_length=2048 \
    data.max_response_length=8192 \
    actor_rollout_ref.model.path=Qwen/Qwen3-4B \
    actor_rollout_ref.rollout.n=4 \
    actor_rollout_ref.rollout.temperature=0.7 \
    actor_rollout_ref.rollout.multi_turn.enable=True \
    actor_rollout_ref.rollout.multi_turn.tool_config_path=verl_tools/tool_config.yaml \
    actor_rollout_ref.rollout.multi_turn.max_assistant_turns=15 \
    actor_rollout_ref.rollout.multi_turn.max_user_turns=15 \
    actor_rollout_ref.actor.use_kl_loss=True \
    actor_rollout_ref.actor.kl_loss_coef=0.04 \
    reward.custom_reward_function.path=verl_tools/ols_reward.py \
    reward.custom_reward_function.name=compute_score \
    trainer.total_epochs=2 \
    trainer.n_gpus_per_node=4 \
    trainer.project_name=ols-grpo \
    trainer.experiment_name=qwen3-4b-grpo
