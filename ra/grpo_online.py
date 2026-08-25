"""Online GRPO training using TRL's GRPOTrainer with environment_factory.

The model generates tool-calling rollouts live against PostgreSQL-backed mock
environments, gets judged by an LLM, and trains with KL-penalized GRPO loss.

Usage:
    cd ra && ../.venv/bin/python grpo_online.py \
        --model Qwen/Qwen3-4B \
        --output-dir grpo-online-qwen3-4b \
        --judge-model gpt-5-mini
"""

import argparse
import json
import logging
import os
import threading
import uuid
from pathlib import Path

import openai
import torch
from datasets import Dataset as HFDataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import GRPOConfig, GRPOTrainer

import db
from eval.judge import ANSWER_CORRECTNESS_PROMPT, _parse_score
from eval.scenarios import SCENARIOS, load_seed
from mock_tools import TOOLS, call_tool, load_tool_defs

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt (matches run_agent.py)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an OpenShift/Kubernetes troubleshooting agent. You have access to MCP \
tools that let you inspect a live cluster: list pods, read logs, check events, \
query Prometheus metrics, inspect alerts, exec into containers, and more.

Your job is to investigate the cluster state using these tools and provide a \
clear, evidence-based diagnosis. Do NOT guess or give generic advice — use the \
tools to gather real data and base your answer on what you find.

Be thorough but efficient. Start broad (check alerts, pod status, events) then \
drill into specific issues you discover."""

# ---------------------------------------------------------------------------
# Thread-local DB connection storage
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def _get_conn():
    """Get the DB connection for the current rollout (stored by env reset)."""
    return getattr(_thread_local, "conn", None)


# ---------------------------------------------------------------------------
# Tool wrapper functions (standalone, for GRPOTrainer tools= parameter)
# ---------------------------------------------------------------------------
# TRL discovers tools via inspect and needs: __name__, docstring, type
# annotations. We load schemas from raw_tool_defs.json and create wrapper
# functions dynamically.


def _build_tool_functions() -> list:
    """Build standalone tool wrapper functions from raw_tool_defs.json.

    Each function accesses the DB via thread-local storage and delegates
    to mock_tools.call_tool().
    """
    raw_path = Path(__file__).parent / "raw_tool_defs.json"
    raw = json.loads(raw_path.read_text())

    functions = []

    for _server, section in raw.items():
        if not isinstance(section, dict) or "tools" not in section:
            continue
        for t in section["tools"]:
            fn_def = t["function"]
            name = fn_def["name"]
            if name not in TOOLS:
                continue

            desc = fn_def.get("description", "")
            params_schema = fn_def.get("parameters", {})
            props = params_schema.get("properties", {})

            # Remove "context" param (not supported by mock tools)
            props = {k: v for k, v in props.items() if k != "context"}

            func = _make_tool_func(name, desc, props)
            functions.append(func)

    # Add extra tools not in raw_tool_defs.json
    for extra_name, extra_desc in [
        ("projects_list", "List all OpenShift projects in the cluster"),
        ("targets_list", "List all Prometheus scrape targets and their status"),
    ]:
        if extra_name in TOOLS and not any(
            f.__name__ == extra_name for f in functions
        ):
            func = _make_tool_func(extra_name, extra_desc, {})
            functions.append(func)

    return functions


def _json_type_to_python(json_type: str) -> str:
    """Map JSON schema type to Python type hint string."""
    return {"integer": "int", "number": "float", "boolean": "bool",
            "array": "list", "object": "dict"}.get(json_type, "str")


def _make_tool_func(name: str, description: str, props: dict):
    """Create a tool function with explicit typed parameters.

    TRL's get_json_schema uses inspect.signature() to extract parameter names
    and types. Functions with **kwargs fail. We use exec() to create functions
    with proper explicit signatures matching the tool's JSON schema.
    """
    # Build parameter list with type annotations and defaults
    param_parts = []
    for pname, pschema in props.items():
        py_type = _json_type_to_python(pschema.get("type", "string"))
        param_parts.append(f"{pname}: {py_type} = None")

    params_str = ", ".join(param_parts)

    # Build docstring with Args section (required by TRL's get_json_schema)
    args_doc = ""
    if props:
        args_lines = []
        for pname, pschema in props.items():
            pdesc = pschema.get("description", f"The {pname} parameter")
            args_lines.append(f"        {pname}: {pdesc}")
        args_doc = "\n\n    Args:\n" + "\n".join(args_lines)

    # Build the function source
    func_source = f"""
def {name}({params_str}) -> str:
    \"\"\"{description}{args_doc}\"\"\"
    conn = _get_conn()
    if conn is None:
        return "Error: no database connection available for tool {name}"
    params = {{k: v for k, v in locals().items() if v is not None and k != "conn"}}
    return str(call_tool(conn, "{name}", params))
"""
    # Execute to create the function in a namespace with our helpers
    namespace = {"_get_conn": _get_conn, "call_tool": call_tool}
    exec(func_source, namespace)  # noqa: S102
    return namespace[name]


# Build tool functions at module level
TOOL_FUNCTIONS = _build_tool_functions()

# ---------------------------------------------------------------------------
# K8sTroubleshootingEnv (environment_factory for TRL)
# ---------------------------------------------------------------------------


class K8sTroubleshootingEnv:
    """TRL environment that manages PostgreSQL-backed K8s scenario lifecycle.

    - reset() is called per rollout: sets up DB with scenario seed data,
      stores connection in thread-local storage, returns the initial query.
    - No tool methods on this class; tools are passed as standalone functions
      via the GRPOTrainer tools= parameter.
    """

    def __init__(self):
        self._db_name: str | None = None
        self._conn = None

    def reset(self, *, prompt: list[dict] | str, scenario_id: str,
              expected_response: str, **kwargs) -> str | None:
        """Set up the DB for this scenario and return observation.

        Called by TRL before each generation. Receives all dataset columns
        as kwargs. Sets up the PostgreSQL database with seed data so tools
        can query it.

        Returns None (TRL will use the prompt as-is since it already contains
        the user query as a chat message).
        """
        # Teardown any previous DB
        self._cleanup()

        # Create a unique DB name for this rollout
        self._db_name = f"grpo_{uuid.uuid4().hex[:12]}"

        # Load seed data and initialize DB
        seed = load_seed(scenario_id)
        db.init_db(seed, db_name=self._db_name)

        # Open connection and store in thread-local for tool functions
        dsn = db._dsn_for(self._db_name)
        import psycopg2
        self._conn = psycopg2.connect(dsn)
        _thread_local.conn = self._conn

        # Return None — the prompt already contains the user query
        return None

    def _cleanup(self):
        """Close connection and drop DB."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

        if self._db_name is not None:
            try:
                db.teardown_db(self._db_name)
            except Exception:
                pass
            self._db_name = None

        _thread_local.conn = None

    def __del__(self):
        self._cleanup()


# ---------------------------------------------------------------------------
# Reward function (LLM judge)
# ---------------------------------------------------------------------------


def _make_judge_reward_func(judge_model: str):
    """Create a reward function that uses an LLM judge to score completions.

    The returned function follows TRL's reward_func contract:
        reward_func(prompts, completions, ..., expected_response, ...) -> list[float]
    """
    client = openai.OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        timeout=120.0,
    )

    def judge_reward(
        prompts,
        completions,
        expected_response: list[str],
        scenario_id: list[str],
        **kwargs,
    ) -> list[float]:
        """Score each completion against the expected response using LLM judge."""
        rewards = []

        for i, (prompt_msgs, completion_msgs) in enumerate(zip(prompts, completions)):
            # Extract the user query from the prompt
            query = ""
            for msg in prompt_msgs:
                if msg.get("role") == "user":
                    query = msg.get("content", "")

            # Extract the final assistant text from completion
            response_text = ""
            for msg in completion_msgs:
                if msg.get("role") == "assistant":
                    content = msg.get("content")
                    if content:
                        response_text = content

            if not response_text:
                rewards.append(0.0)
                continue

            # Call judge LLM
            try:
                judge_prompt = ANSWER_CORRECTNESS_PROMPT.format(
                    query=query,
                    response=response_text,
                    expected_response=expected_response[i],
                )

                judge_response = client.chat.completions.create(
                    model=judge_model,
                    messages=[
                        {"role": "system", "content": "You are an evaluation judge."},
                        {"role": "user", "content": judge_prompt},
                    ],
                    max_completion_tokens=1024,
                )

                raw = judge_response.choices[0].message.content or ""
                score = _parse_score(raw)
                rewards.append(score)

            except Exception as e:
                logger.warning(f"Judge failed for completion {i}: {e}")
                rewards.append(0.0)

        return rewards

    return judge_reward


# ---------------------------------------------------------------------------
# Dataset construction
# ---------------------------------------------------------------------------


def build_dataset(scenarios: list[str] | None = None, repeats: int = 50) -> HFDataset:
    """Build a HuggingFace Dataset for GRPOTrainer.

    Each row has:
    - prompt: list of chat messages (system + user query)
    - scenario_id: which scenario this is from
    - expected_response: for the judge reward function

    For multi-turn scenarios (wrong_networkpolicy), only the first turn is used
    since TRL generates single completions per prompt.

    Args:
        scenarios: list of scenario IDs to include (None = all)
        repeats: how many times to repeat each scenario in the dataset
    """
    rows = []

    scenario_ids = scenarios or sorted(SCENARIOS.keys())

    for sid in scenario_ids:
        scenario = SCENARIOS[sid]
        # Use first turn for all scenarios
        turn = scenario.turns[0]

        for _ in range(repeats):
            rows.append({
                "prompt": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": turn.query},
                ],
                "scenario_id": sid,
                "expected_response": turn.expected_response,
            })

    return HFDataset.from_list(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Online GRPO training with TRL environment_factory"
    )
    parser.add_argument(
        "--model", type=str, default="Qwen/Qwen3-4B",
        help="Model name or path"
    )
    parser.add_argument(
        "--output-dir", type=str, default="grpo-online-qwen3-4b",
        help="Output directory for checkpoints"
    )
    parser.add_argument(
        "--repeats", type=int, default=50,
        help="Number of times to repeat each scenario in dataset"
    )
    parser.add_argument(
        "--num-generations", type=int, default=4,
        help="Number of completions per prompt"
    )
    parser.add_argument(
        "--epochs", type=int, default=2,
        help="Number of training epochs"
    )
    parser.add_argument(
        "--lr", type=float, default=1e-6,
        help="Learning rate"
    )
    parser.add_argument(
        "--beta", type=float, default=0.04,
        help="KL penalty coefficient"
    )
    parser.add_argument(
        "--judge-model", type=str, default="gpt-5-mini",
        help="Model to use for LLM judge scoring"
    )
    parser.add_argument(
        "--max-tool-iterations", type=int, default=15,
        help="Maximum tool-calling iterations per generation"
    )
    parser.add_argument(
        "--scenarios", type=str, nargs="*", default=None,
        help="Specific scenarios to train on (default: all)"
    )
    parser.add_argument(
        "--per-device-batch-size", type=int, default=4,
        help="Per-device training batch size"
    )
    parser.add_argument(
        "--gradient-accumulation-steps", type=int, default=4,
        help="Gradient accumulation steps"
    )
    parser.add_argument(
        "--max-completion-length", type=int, default=4096,
        help="Maximum completion length in tokens"
    )
    parser.add_argument(
        "--lora", action="store_true",
        help="Use LoRA for parameter-efficient training"
    )
    parser.add_argument(
        "--bf16", action="store_true", default=True,
        help="Use bfloat16 precision"
    )
    parser.add_argument(
        "--use-vllm", action="store_true",
        help="Use vLLM for generation (requires vLLM on a separate GPU)"
    )
    return parser.parse_args()


def main():
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    logger.info(f"Loading model: {args.model}")

    # Model
    model_kwargs = {
        "torch_dtype": torch.bfloat16 if args.bf16 else torch.float32,
        "attn_implementation": "sdpa",
        "trust_remote_code": True,
    }

    # Tokenizer
    tokenizer = AutoTokenizer.from_pretrained(
        args.model,
        trust_remote_code=True,
        padding_side="left",
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Dataset
    logger.info("Building dataset...")
    dataset = build_dataset(scenarios=args.scenarios, repeats=args.repeats)
    logger.info(f"Dataset size: {len(dataset)} rows")

    # Reward function
    judge_reward = _make_judge_reward_func(args.judge_model)

    # LoRA config
    peft_config = None
    if args.lora:
        from peft import LoraConfig
        peft_config = LoraConfig(
            r=16,
            lora_alpha=32,
            lora_dropout=0.05,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                            "gate_proj", "up_proj", "down_proj"],
            task_type="CAUSAL_LM",
        )

    # GRPOConfig
    config = GRPOConfig(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.per_device_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.lr,
        beta=args.beta,
        num_generations=args.num_generations,
        max_completion_length=args.max_completion_length,
        max_tool_calling_iterations=args.max_tool_iterations,
        bf16=args.bf16,
        gradient_checkpointing=True,
        logging_steps=1,
        save_strategy="epoch",
        report_to="none",
        remove_unused_columns=False,
        model_init_kwargs=model_kwargs,
        # Use temperature > 0 for diverse generations
        temperature=0.7,
        # vLLM for generation (separate GPU, avoids OOM)
        use_vllm=args.use_vllm,
        vllm_gpu_memory_utilization=0.7 if args.use_vllm else None,
    )

    # Trainer
    trainer = GRPOTrainer(
        model=args.model,
        args=config,
        train_dataset=dataset,
        processing_class=tokenizer,
        reward_funcs=[judge_reward],
        tools=TOOL_FUNCTIONS,
        environment_factory=K8sTroubleshootingEnv,
        peft_config=peft_config,
    )

    logger.info("Starting training...")
    trainer.train()

    # Save final model
    trainer.save_model(args.output_dir)
    logger.info(f"Model saved to {args.output_dir}")


if __name__ == "__main__":
    main()
