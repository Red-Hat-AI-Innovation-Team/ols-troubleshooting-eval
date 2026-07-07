"""Stress test: multi-turn gpt-oss tool calling.

Simulates the actual agent loop: user query → tool call → fake tool result → 
next turn → repeat. Checks each turn for harmony token leakage and parsing
failures.

vLLM server command (node 05, vllm_venv_new = vLLM 0.19.1):
    export HF_HOME=/mnt/nvme0n1/rawhad/hf_cache
    CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \\
    ~/rawhad/vllm_venv_new/bin/vllm serve openai/gpt-oss-20b \\
      --served-model-name model \\
      --tensor-parallel-size 8 \\
      --max-model-len 131072 \\
      --port 8000 \\
      --tool-call-parser openai \\
      --reasoning-parser openai_gptoss \\
      --enable-auto-tool-choice
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import openai

SGLANG_URL = "http://10.241.128.21:8000/v1"  # node 05 vLLM 0.19.1
MODEL = "model"
N_CONVERSATIONS = 20
CONCURRENCY = 5
MAX_TURNS = 10

HARMONY_RE = re.compile(r"<\|(?:start|end|channel|constrain|message|call)\|>")

SYSTEM = "You are a Kubernetes troubleshooting assistant. Use the available tools to investigate cluster issues. Gather information step by step using the tools available. After gathering enough information, provide your diagnosis."

TOOLS = [
    {"type": "function", "function": {"name": "pods_list", "description": "List pods in a namespace", "parameters": {"type": "object", "properties": {"namespace": {"type": "string"}, "labelSelector": {"type": "string"}, "fieldSelector": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "events_list", "description": "List events in a namespace", "parameters": {"type": "object", "properties": {"namespace": {"type": "string"}, "fieldSelector": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "pods_log", "description": "Get logs from a pod", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "namespace": {"type": "string"}, "tail": {"type": "integer"}, "previous": {"type": "boolean"}}, "required": ["name", "namespace"]}}},
    {"type": "function", "function": {"name": "resources_get", "description": "Get a Kubernetes resource by kind/name/namespace", "parameters": {"type": "object", "properties": {"apiVersion": {"type": "string"}, "kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}, "required": ["kind", "name"]}}},
    {"type": "function", "function": {"name": "list_metrics", "description": "List available Prometheus metrics matching a regex", "parameters": {"type": "object", "properties": {"name_regex": {"type": "string"}}, "required": ["name_regex"]}}},
]

FAKE_TOOL_RESULTS = {
    "pods_list": json.dumps([
        {"name": "api-deploy-7b4d6f-kx2tn", "namespace": "payments", "status": "CrashLoopBackOff", "restarts": 14, "ready": "0/1"},
        {"name": "api-deploy-7b4d6f-m3q9r", "namespace": "payments", "status": "Running", "restarts": 0, "ready": "1/1"},
        {"name": "db-pg-0", "namespace": "payments", "status": "Running", "restarts": 0, "ready": "1/1"},
    ]),
    "events_list": json.dumps([
        {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container api-server in pod api-deploy-7b4d6f-kx2tn", "count": 14, "lastTimestamp": "2024-01-15T10:32:00Z"},
        {"type": "Warning", "reason": "Unhealthy", "message": "Readiness probe failed: HTTP probe failed with statuscode: 503", "count": 28, "lastTimestamp": "2024-01-15T10:31:55Z"},
    ]),
    "pods_log": "2024-01-15T10:30:01Z ERROR [db-pool] Failed to acquire connection from pool within 5000ms\n2024-01-15T10:30:01Z FATAL [main] Shutting down due to database connection failure, exit code 1\n2024-01-15T10:29:55Z INFO [db-pool] Initializing connection pool: size=20, timeout=5000ms\n2024-01-15T10:29:55Z INFO [main] Starting API server on :8080",
    "resources_get": json.dumps({"kind": "Deployment", "metadata": {"name": "api-deploy", "namespace": "payments"}, "spec": {"replicas": 3}, "status": {"availableReplicas": 1, "unavailableReplicas": 2}}),
    "list_metrics": json.dumps(["pgbouncer_pools_server_active_connections", "pgbouncer_pools_client_waiting_connections", "pgbouncer_stats_queries_total"]),
}

QUERIES = [
    "The pods in the payments namespace are crashlooping. What's going on?",
    "I'm seeing 503 errors from the ecommerce API. Can you investigate?",
    "kube-apiserver on master-0 is degraded, check what's happening",
    "There are eviction events in the monitoring namespace, dig into it",
    "Our database pods keep restarting with OOMKilled, check the logs",
]


@dataclass
class TurnResult:
    turn: int
    has_tool_calls: bool
    tool_names: list[str]
    content: str | None
    finish_reason: str | None
    harmony_in_content: bool
    harmony_in_tool_name: bool
    error: str | None = None


@dataclass 
class ConversationResult:
    idx: int
    query: str
    turns: list[TurnResult]
    total_turns: int = 0
    ended_with_text: bool = False
    any_harmony: bool = False
    error: str | None = None
    latency_s: float = 0.0


def run_conversation(idx: int) -> ConversationResult:
    query = QUERIES[idx % len(QUERIES)]
    client = openai.OpenAI(api_key="not-needed", base_url=SGLANG_URL)
    
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": query},
    ]
    
    turns: list[TurnResult] = []
    t0 = time.time()
    
    for turn_num in range(MAX_TURNS):
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            max_completion_tokens=4000,
        )
        
        choice = resp.choices[0]
        msg = choice.message
        content = msg.content
        tool_calls = msg.tool_calls or []
        tool_names = [tc.function.name for tc in tool_calls]
        
        harmony_in_content = bool(content and HARMONY_RE.search(content))
        harmony_in_tool_name = any(HARMONY_RE.search(n) for n in tool_names)
        
        tr = TurnResult(
            turn=turn_num,
            has_tool_calls=len(tool_calls) > 0,
            tool_names=tool_names,
            content=content[:200] if content else None,
            finish_reason=choice.finish_reason,
            harmony_in_content=harmony_in_content,
            harmony_in_tool_name=harmony_in_tool_name,
        )
        turns.append(tr)
        
        if not tool_calls:
            # Agent is done — final text answer
            return ConversationResult(
                idx=idx, query=query, turns=turns,
                total_turns=turn_num + 1,
                ended_with_text=True,
                any_harmony=any(t.harmony_in_content or t.harmony_in_tool_name for t in turns),
                latency_s=time.time() - t0,
            )
        
        # Build assistant message with tool calls
        assistant_msg: dict = {"role": "assistant", "content": content}
        assistant_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in tool_calls
        ]
        messages.append(assistant_msg)
        
        # Add fake tool results
        for tc in tool_calls:
            fake_result = FAKE_TOOL_RESULTS.get(tc.function.name, '{"error": "unknown tool"}')
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": fake_result,
            })
    
    return ConversationResult(
        idx=idx, query=query, turns=turns,
        total_turns=MAX_TURNS,
        ended_with_text=False,
        any_harmony=any(t.harmony_in_content or t.harmony_in_tool_name for t in turns),
        latency_s=time.time() - t0,
    )


def main():
    print(f"Multi-turn stress test: {N_CONVERSATIONS} conversations, concurrency={CONCURRENCY}, max_turns={MAX_TURNS}")
    print(f"Server: {SGLANG_URL}")
    print("=" * 70)

    results: list[ConversationResult] = []

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(run_conversation, i): i for i in range(N_CONVERSATIONS)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                r = fut.result()
                results.append(r)
                print(f"  [{r.idx}] {r.total_turns} turns, ended_text={r.ended_with_text}, harmony={r.any_harmony}")
            except Exception as e:
                results.append(ConversationResult(
                    idx=idx, query=QUERIES[idx % len(QUERIES)],
                    turns=[], error=f"{type(e).__name__}: {e}",
                ))
                print(f"  [{idx}] ERROR: {type(e).__name__}: {str(e)[:100]}")

    results.sort(key=lambda r: r.idx)

    # --- Analysis ---
    print("\n" + "=" * 70)
    print("ANALYSIS")
    print("=" * 70)

    n_ok = sum(1 for r in results if not r.error)
    n_ended_text = sum(1 for r in results if r.ended_with_text)
    n_hit_max = sum(1 for r in results if not r.ended_with_text and not r.error)
    n_harmony = sum(1 for r in results if r.any_harmony)
    n_errors = sum(1 for r in results if r.error)
    turn_counts = [r.total_turns for r in results if not r.error]

    all_turns = [t for r in results for t in r.turns]
    n_turns_with_tools = sum(1 for t in all_turns if t.has_tool_calls)
    n_turns_text = sum(1 for t in all_turns if not t.has_tool_calls)
    n_turns_harmony_content = sum(1 for t in all_turns if t.harmony_in_content)
    n_turns_harmony_tool = sum(1 for t in all_turns if t.harmony_in_tool_name)

    print(f"\nConversations:           {N_CONVERSATIONS}")
    print(f"  Completed OK:          {n_ok}")
    print(f"  Ended with text:       {n_ended_text}")
    print(f"  Hit max turns:         {n_hit_max}")
    print(f"  Any harmony leakage:   {n_harmony}")
    print(f"  Errors:                {n_errors}")
    if turn_counts:
        print(f"  Avg turns:             {sum(turn_counts)/len(turn_counts):.1f}")

    print(f"\nTotal turns:             {len(all_turns)}")
    print(f"  With tool calls:       {n_turns_with_tools}")
    print(f"  Text-only:             {n_turns_text}")
    print(f"  Harmony in content:    {n_turns_harmony_content}")
    print(f"  Harmony in tool name:  {n_turns_harmony_tool}")

    # Show problematic conversations
    bad = [r for r in results if r.any_harmony or r.error]
    if bad:
        print(f"\n--- Problematic conversations ({len(bad)}) ---")
        for r in bad[:10]:
            print(f"\n[{r.idx}] query: {r.query[:60]}")
            print(f"     turns: {r.total_turns}, ended_text: {r.ended_with_text}")
            if r.error:
                print(f"     error: {r.error[:200]}")
            for t in r.turns:
                flag = ""
                if t.harmony_in_content:
                    flag += " [HARMONY_CONTENT]"
                if t.harmony_in_tool_name:
                    flag += " [HARMONY_TOOL]"
                if t.has_tool_calls:
                    print(f"     turn {t.turn}: tools={t.tool_names} finish={t.finish_reason}{flag}")
                else:
                    print(f"     turn {t.turn}: text={t.content[:100] if t.content else None} finish={t.finish_reason}{flag}")
    else:
        print("\nNo problematic conversations!")


if __name__ == "__main__":
    main()
