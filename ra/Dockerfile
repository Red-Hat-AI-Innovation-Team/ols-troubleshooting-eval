# OLS GRPO Training Container
# Includes verl, vllm, and all dependencies for multi-turn agentic GRPO training
#
# Build:
#   docker build -t ols-grpo:latest -f ra/Dockerfile .
#
# Run (4x GPU):
#   docker run --gpus '"device=4,5,6,7"' \
#     -v $(pwd)/ra:/workspace/ra \
#     -v /path/to/checkpoints:/workspace/checkpoints \
#     -e OPENAI_API_KEY=$OPENAI_API_KEY \
#     -e BASE_MODEL=Qwen/Qwen3-4B \
#     ols-grpo:latest
#
# Run with custom args:
#   docker run --gpus '"device=4,5,6,7"' \
#     -v $(pwd)/ra:/workspace/ra \
#     -e OPENAI_API_KEY=$OPENAI_API_KEY \
#     ols-grpo:latest \
#     trainer.total_epochs=1

FROM nvcr.io/nvidia/pytorch:24.12-py3

# System deps for PostgreSQL client (mock tools need psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Python deps
RUN pip install --no-cache-dir \
    verl==0.8.0 \
    vllm==0.11.0 \
    psycopg2-binary \
    openai \
    datasets \
    peft \
    flash-attn --no-build-isolation

# Ray 2.56 uv hook fix
ENV RAY_ENABLE_UV_RUN_RUNTIME_ENV=0

WORKDIR /workspace/ra

# Copy training code
COPY ra/ /workspace/ra/

# Generate tool config at build time (doesn't need DB)
RUN python verl_tools/generate_tool_config.py

# Default: run training
ENTRYPOINT ["bash", "verl_train.sh"]
