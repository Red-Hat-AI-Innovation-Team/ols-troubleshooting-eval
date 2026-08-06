#!/usr/bin/env python3
"""
vLLM Streaming Proxy

Intercepts stream=true chat completion requests, performs them as
stream=false against the vLLM backend, and converts the response
back to SSE chunks. All other requests pass through transparently.

Workaround for harmony token leakage in vLLM streaming mode
(affects GPT-OSS-20B and similar models).

Usage:
    # Run proxy
    python vllm_proxy.py --backend http://10.241.128.21:8000 --port 8200

    # Run tests
    python vllm_proxy.py test
"""

import argparse
import asyncio
import copy
import json
import re
import time
import uuid
from typing import AsyncGenerator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response


HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-length", "content-encoding", "host",
})


MAX_RETRIES = 5
HARMONY_ERROR_MARKER = "unexpected tokens remaining in message header"
_HARMONY_FUNC_STRIP_RE = re.compile(r'<\|[^|]*\|>.*')
_HARMONY_TOKEN_RE = re.compile(r'<\|[^|]*\|>')


def _is_harmony_error(resp: httpx.Response) -> bool:
    """Check if a vLLM response is a harmony parser crash (retryable)."""
    if resp.status_code != 500:
        return False
    try:
        return HARMONY_ERROR_MARKER in resp.text
    except Exception:
        return False


def _clean_func_name(name: str) -> str:
    return _HARMONY_FUNC_STRIP_RE.sub('', name)


def _clean_response(completion: dict) -> dict:
    for choice in completion.get("choices", []):
        msg = choice.get("message", {})
        for tc in msg.get("tool_calls", []):
            func = tc.get("function", {})
            if "name" in func:
                func["name"] = _clean_func_name(func["name"])
    return completion


def _clean_request(data: dict) -> dict:
    for msg in data.get("messages", []):
        for tc in msg.get("tool_calls", []):
            func = tc.get("function", {})
            if "name" in func:
                func["name"] = _clean_func_name(func["name"])
        if msg.get("role") == "tool" and "name" in msg:
            msg["name"] = _clean_func_name(msg["name"])
        content = msg.get("content")
        if isinstance(content, str) and "<|" in content:
            msg["content"] = _HARMONY_TOKEN_RE.sub('', content)
    return data


def _truncate_history(data: dict, drop_pairs: int) -> dict:
    msgs = data.get("messages", [])
    system = [m for m in msgs if m.get("role") == "system"]
    rest = [m for m in msgs if m.get("role") != "system"]
    drop_count = min(drop_pairs * 2, max(0, len(rest) - 2))
    data["messages"] = system + rest[drop_count:]
    return data


def create_app(backend_url: str, timeout: float = 300.0) -> FastAPI:
    app = FastAPI()

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    )
    async def proxy(request: Request, path: str):
        body = await request.body()

        is_stream_intercept = False
        original_stream_options = None
        is_chat_completion = (
            path in ("v1/chat/completions", "v1/completions")
            and request.method == "POST"
            and body
        )

        data = None
        if is_chat_completion:
            try:
                data = json.loads(body)
                _clean_request(data)
                if data.get("stream", False):
                    is_stream_intercept = True
                    original_stream_options = data.pop("stream_options", None)
                    data["stream"] = False
                body = json.dumps(data).encode()
            except (json.JSONDecodeError, KeyError):
                pass

        fwd_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP
        }

        url = f"{backend_url}/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        max_attempts = MAX_RETRIES if is_chat_completion else 1
        resp: httpx.Response | None = None
        retry_body = body
        for attempt in range(max_attempts):
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                resp = await client.request(
                    method=request.method,
                    url=url,
                    content=retry_body,
                    headers=fwd_headers,
                )
            if not _is_harmony_error(resp):
                break
            print(f"[proxy] harmony error on attempt {attempt + 1}/{max_attempts}, retrying with truncated history...")
            if data is not None:
                retry_data = copy.deepcopy(data)
                _truncate_history(retry_data, drop_pairs=attempt + 1)
                retry_body = json.dumps(retry_data).encode()
            await asyncio.sleep(0.5)

        assert resp is not None

        if is_stream_intercept and resp.status_code == 200:
            completion = _clean_response(resp.json())
            include_usage = bool(
                original_stream_options
                and original_stream_options.get("include_usage", False)
            )
            return StreamingResponse(
                completion_to_sse(completion, include_usage=include_usage),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        if is_chat_completion and resp.status_code == 200:
            completion = _clean_response(resp.json())
            resp_headers = {
                k: v
                for k, v in resp.headers.items()
                if k.lower() not in HOP_BY_HOP
            }
            return Response(
                content=json.dumps(completion).encode(),
                status_code=resp.status_code,
                headers=resp_headers,
            )

        # Passthrough
        resp_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in HOP_BY_HOP
        }
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
        )

    return app


async def completion_to_sse(
    completion: dict,
    include_usage: bool = False,
) -> AsyncGenerator[str, None]:
    """Convert a chat.completion response to chat.completion.chunk SSE events."""
    chunk_id = completion.get("id", f"chatcmpl-{uuid.uuid4().hex[:12]}")
    created = completion.get("created", int(time.time()))
    model = completion.get("model", "unknown")

    for choice in completion.get("choices", []):
        idx = choice.get("index", 0)
        msg = choice.get("message", {})
        finish = choice.get("finish_reason", "stop")

        # 1) Role chunk
        yield _sse({
            "id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": idx, "delta": {"role": msg.get("role", "assistant")}, "finish_reason": None}],
        })

        # 2) Content chunk
        content = msg.get("content")
        if content:
            yield _sse({
                "id": chunk_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": idx, "delta": {"content": content}, "finish_reason": None}],
            })

        # 3) Tool call chunks
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc_idx, tc in enumerate(tool_calls):
                tc_delta = {
                    "index": tc_idx,
                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                yield _sse({
                    "id": chunk_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": idx, "delta": {"tool_calls": [tc_delta]}, "finish_reason": None}],
                })

        # 4) Finish chunk
        yield _sse({
            "id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": idx, "delta": {}, "finish_reason": finish}],
        })

    # 5) Usage chunk
    if include_usage and "usage" in completion:
        yield _sse({
            "id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [], "usage": completion["usage"],
        })

    yield "data: [DONE]\n\n"


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _run_tests():
    async def _test_all():
        passed = 0
        failed = 0

        # --- Test 1: text response ---
        print("Test 1: completion_to_sse — text response")
        completion = {
            "id": "chatcmpl-test1",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello, world!"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        chunks = [c async for c in completion_to_sse(completion)]
        # Expect: role, content, finish, [DONE]
        assert len(chunks) == 4, f"expected 4 chunks, got {len(chunks)}"
        assert '"role": "assistant"' in chunks[0]
        assert '"content": "Hello, world!"' in chunks[1]
        assert '"finish_reason": "stop"' in chunks[2]
        assert chunks[3] == "data: [DONE]\n\n"
        for c in chunks[:3]:
            assert c.startswith("data: ") and c.endswith("\n\n")
        print("  PASS")
        passed += 1

        # --- Test 2: tool call response ---
        print("Test 2: completion_to_sse — tool call response")
        completion_tc = {
            "id": "chatcmpl-test2",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "get_pods", "arguments": '{"namespace":"default"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }
        chunks = [c async for c in completion_to_sse(completion_tc)]
        # role, tool_call, finish, [DONE] (no content chunk — content is None)
        assert len(chunks) == 4, f"expected 4 chunks, got {len(chunks)}"
        assert '"get_pods"' in chunks[1]
        assert '"call_abc"' in chunks[1]
        assert '"finish_reason": "tool_calls"' in chunks[2]
        print("  PASS")
        passed += 1

        # --- Test 3: multiple tool calls ---
        print("Test 3: completion_to_sse — multiple tool calls")
        completion_multi = {
            "id": "chatcmpl-test3",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Let me check both.",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "get_pods", "arguments": "{}"}},
                        {"id": "call_2", "type": "function", "function": {"name": "get_events", "arguments": "{}"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }],
        }
        chunks = [c async for c in completion_to_sse(completion_multi)]
        # role, content, tool_call_1, tool_call_2, finish, [DONE]
        assert len(chunks) == 6, f"expected 6 chunks, got {len(chunks)}"
        assert '"content": "Let me check both."' in chunks[1]
        assert '"call_1"' in chunks[2]
        assert '"call_2"' in chunks[3]
        print("  PASS")
        passed += 1

        # --- Test 4: include_usage ---
        print("Test 4: completion_to_sse — include_usage")
        chunks = [c async for c in completion_to_sse(completion, include_usage=True)]
        # role, content, finish, usage, [DONE]
        assert len(chunks) == 5, f"expected 5 chunks, got {len(chunks)}"
        assert '"usage"' in chunks[3]
        assert '"prompt_tokens": 10' in chunks[3]
        print("  PASS")
        passed += 1

        # --- Test 5: chunks are valid JSON ---
        print("Test 5: all SSE data lines are valid JSON")
        chunks = [c async for c in completion_to_sse(completion)]
        for c in chunks:
            if c == "data: [DONE]\n\n":
                continue
            payload = c.removeprefix("data: ").rstrip("\n")
            parsed = json.loads(payload)
            assert parsed["object"] == "chat.completion.chunk"
        print("  PASS")
        passed += 1

        # --- Test 6: _clean_func_name ---
        print("Test 6: _clean_func_name strips harmony tokens")
        assert _clean_func_name("pods_get") == "pods_get"
        assert _clean_func_name("pods_get<|channel|>commentary") == "pods_get"
        assert _clean_func_name("resources_get<|channel|>json") == "resources_get"
        assert _clean_func_name("events_list<|channel|>") == "events_list"
        assert _clean_func_name("events_list<|channel|>analysis") == "events_list"
        assert _clean_func_name("configmaps_get>commentary") == "configmaps_get>commentary"
        print("  PASS")
        passed += 1

        # --- Test 7: _clean_response ---
        print("Test 7: _clean_response cleans tool call function names")
        dirty_resp = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"function": {"name": "pods_get<|channel|>commentary", "arguments": "{}"}},
                        {"function": {"name": "events_list<|channel|>analysis", "arguments": "{}"}},
                    ],
                },
            }],
        }
        _clean_response(dirty_resp)
        assert dirty_resp["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "pods_get"
        assert dirty_resp["choices"][0]["message"]["tool_calls"][1]["function"]["name"] == "events_list"
        print("  PASS")
        passed += 1

        # --- Test 8: _clean_request ---
        print("Test 8: _clean_request cleans conversation history")
        dirty_req = {
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "List pods"},
                {"role": "assistant", "tool_calls": [
                    {"function": {"name": "pods_list<|channel|>commentary", "arguments": "{}"}}
                ]},
                {"role": "tool", "name": "pods_list<|channel|>commentary", "content": "pod1, pod2"},
                {"role": "assistant", "content": "Found pods<|end|><|start|>assistant"},
            ],
        }
        _clean_request(dirty_req)
        assert dirty_req["messages"][2]["tool_calls"][0]["function"]["name"] == "pods_list"
        assert dirty_req["messages"][3]["name"] == "pods_list"
        assert "<|" not in dirty_req["messages"][4]["content"]
        print("  PASS")
        passed += 1

        # --- Test 9: _truncate_history ---
        print("Test 9: _truncate_history drops oldest non-system messages")
        hist = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
                {"role": "assistant", "content": "a2"},
                {"role": "user", "content": "q3"},
                {"role": "assistant", "content": "a3"},
            ],
        }
        t1 = _truncate_history(copy.deepcopy(hist), drop_pairs=1)
        assert len(t1["messages"]) == 5
        assert t1["messages"][0]["role"] == "system"
        assert t1["messages"][1]["content"] == "q2"
        t2 = _truncate_history(copy.deepcopy(hist), drop_pairs=2)
        assert len(t2["messages"]) == 3
        assert t2["messages"][1]["content"] == "q3"
        t3 = _truncate_history(copy.deepcopy(hist), drop_pairs=10)
        assert len(t3["messages"]) == 3
        print("  PASS")
        passed += 1

        # --- Test 10: SSE with cleaned leaked names ---
        print("Test 10: completion_to_sse with cleaned leaked names")
        leaked_completion = {
            "id": "chatcmpl-leaked", "object": "chat.completion",
            "created": 1700000000, "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "call_x", "type": "function",
                        "function": {"name": "pods_get<|channel|>commentary", "arguments": '{}'}}],
                },
                "finish_reason": "tool_calls",
            }],
        }
        cleaned_lc = _clean_response(copy.deepcopy(leaked_completion))
        chunks = [c async for c in completion_to_sse(cleaned_lc)]
        assert '"pods_get"' in chunks[1]
        assert "<|channel|>" not in chunks[1]
        print("  PASS")
        passed += 1

        # --- Test 11: integration with mock backend ---
        print("Test 11: integration — proxy with mock backend")

        mock_app = FastAPI()

        @mock_app.post("/v1/chat/completions")
        async def mock_chat(request: Request):
            body = await request.json()
            # Proxy must have flipped stream to false
            assert body.get("stream") is False, f"stream was {body.get('stream')}"
            return completion

        @mock_app.get("/v1/models")
        async def mock_models():
            return {"data": [{"id": "test-model"}]}

        mock_cfg = uvicorn.Config(mock_app, host="127.0.0.1", port=18901, log_level="error")
        mock_srv = uvicorn.Server(mock_cfg)

        proxy_app = create_app("http://127.0.0.1:18901")
        proxy_cfg = uvicorn.Config(proxy_app, host="127.0.0.1", port=18902, log_level="error")
        proxy_srv = uvicorn.Server(proxy_cfg)

        mock_task = asyncio.create_task(mock_srv.serve())
        proxy_task = asyncio.create_task(proxy_srv.serve())
        await asyncio.sleep(1.5)

        async with httpx.AsyncClient() as client:
            # 6a: streaming request -> intercepted, returned as SSE
            resp = await client.post(
                "http://127.0.0.1:18902/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
                timeout=10.0,
            )
            assert resp.status_code == 200, f"status {resp.status_code}"
            assert "text/event-stream" in resp.headers["content-type"]
            assert "data: [DONE]" in resp.text
            assert '"Hello, world!"' in resp.text
            print("  6a: streaming interception — PASS")
            passed += 1

            # 6b: non-streaming request -> passthrough
            resp = await client.post(
                "http://127.0.0.1:18902/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "stream": False},
                timeout=10.0,
            )
            assert resp.status_code == 200
            assert resp.json()["id"] == "chatcmpl-test1"
            print("  6b: non-streaming passthrough — PASS")
            passed += 1

            # 6c: GET /v1/models -> passthrough
            resp = await client.get("http://127.0.0.1:18902/v1/models", timeout=10.0)
            assert resp.status_code == 200
            assert resp.json()["data"][0]["id"] == "test-model"
            print("  6c: GET passthrough — PASS")
            passed += 1

        mock_srv.should_exit = True
        proxy_srv.should_exit = True
        await asyncio.gather(mock_task, proxy_task, return_exceptions=True)

        print(f"\n{'='*40}")
        print(f"Results: {passed} passed, {failed} failed")

    asyncio.run(_test_all())


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        _run_tests()
    else:
        parser = argparse.ArgumentParser(description="vLLM streaming proxy")
        parser.add_argument("--backend", default="http://localhost:8000", help="vLLM backend URL")
        parser.add_argument("--port", type=int, default=8200, help="Proxy listen port")
        parser.add_argument("--host", default="0.0.0.0", help="Proxy listen host")
        parser.add_argument("--timeout", type=float, default=300.0, help="Backend timeout (seconds)")
        args = parser.parse_args()

        app = create_app(args.backend, timeout=args.timeout)
        print(f"Proxy listening on {args.host}:{args.port} -> {args.backend}")
        uvicorn.run(app, host=args.host, port=args.port)
