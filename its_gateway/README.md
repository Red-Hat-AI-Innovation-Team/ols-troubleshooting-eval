# ITS Gateway

Vendored copy of `its_hub`'s IaaS server with SSE streaming support.

This is the OpenAI-compatible gateway that sits between OLS and the LLM
for inference-time scaling. When `ITS_BUDGET` is set, `run_eval.sh`
starts this gateway and points OLS at it.

## Why vendored

The PyPI release of `its-hub` (v1.0.0) doesn't include the IaaS module.
The dev version has it but doesn't support SSE streaming, which OLS
requires (LangChain uses `astream()` internally). This copy adds
streaming support.

## Changes from upstream

- Added `_stream_chat_completions()` for SSE streaming responses
- Changed `budget` field to use configured default when not in request
- Added `CONFIGURED_BUDGET` global set via `/configure`

## Installation

```bash
# Install its_hub core (needed for algorithms/types)
cd lightspeed-service && uv pip install its-hub

# The iaas.py in this directory is used directly by run_eval.sh
```
