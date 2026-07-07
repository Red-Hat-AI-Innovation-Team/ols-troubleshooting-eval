# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
#
# Patched openai tool parser for gpt-oss harmony token stripping.
# Copy this file over the installed vLLM parser:
#   cp openai_tool_parser.py \
#     <venv>/lib/python3.12/site-packages/vllm/tool_parsers/openai_tool_parser.py
#
# vLLM server command (node 05, vllm_venv_new = vLLM 0.19.1):
#   export HF_HOME=/mnt/nvme0n1/rawhad/hf_cache
#   CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
#   ~/rawhad/vllm_venv_new/bin/vllm serve openai/gpt-oss-20b \
#     --served-model-name model \
#     --tensor-parallel-size 8 \
#     --max-model-len 131072 \
#     --port 8000 \
#     --tool-call-parser openai \
#     --reasoning-parser openai_gptoss \
#     --enable-auto-tool-choice
import json
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from vllm.entrypoints.openai.chat_completion.protocol import (
    ChatCompletionRequest,
)
from vllm.entrypoints.openai.engine.protocol import (
    DeltaMessage,
    ExtractedToolCallInformation,
    FunctionCall,
    ToolCall,
)
from vllm.entrypoints.openai.parser.harmony_utils import parse_output_into_messages
from vllm.logger import init_logger
from vllm.tool_parsers.abstract_tool_parser import (
    Tool,
    ToolParser,
)

if TYPE_CHECKING:
    from vllm.tokenizers import TokenizerLike
else:
    TokenizerLike = object

logger = init_logger(__name__)

# (rohan): strip harmony special tokens leaked into msg.recipient by openai_harmony
_HARMONY_TOKEN_RE = re.compile(r'<\|[^|]+\|>.*')


class OpenAIToolParser(ToolParser):
    def __init__(self, tokenizer: "TokenizerLike", tools: list[Tool] | None = None):
        super().__init__(tokenizer, tools)

    def extract_tool_calls(
        self,
        model_output: str,
        request: ChatCompletionRequest,
        token_ids: Sequence[int] | None = None,
    ) -> ExtractedToolCallInformation:
        if token_ids is None:
            raise NotImplementedError(
                "OpenAIToolParser requires token IDs and does not support text-based extraction."  # noqa: E501
            )

        parser = parse_output_into_messages(token_ids)
        tool_calls = []
        final_content = None
        commentary_content = None

        if len(parser.messages) > 0:
            for msg in parser.messages:
                if len(msg.content) < 1:
                    continue
                msg_text = msg.content[0].text
                if msg.recipient and msg.recipient.startswith("functions."):
                    # If no content-type is given assume JSON, as that's the
                    # most common case with gpt-oss models.
                    if not msg.content_type or "json" in msg.content_type:
                        # load and dump the JSON text to check validity and
                        # remove any extra newlines or other odd formatting
                        try:
                            tool_args = json.dumps(json.loads(msg_text))
                        except json.JSONDecodeError:
                            logger.exception(
                                "Error decoding JSON tool call from response."
                            )
                            tool_args = msg_text
                    else:
                        tool_args = msg_text
                    raw_name = msg.recipient.split("functions.")[1]
                    # (rohan): strip leaked harmony tokens from function name
                    # e.g. "pods_list<|channel|>commentary" → "pods_list"
                    clean_name = _HARMONY_TOKEN_RE.sub("", raw_name)
                    tool_calls.append(
                        ToolCall(
                            type="function",
                            function=FunctionCall(
                                name=clean_name,
                                arguments=tool_args,
                            ),
                        )
                    )
                elif msg.channel == "final":
                    final_content = msg_text
                elif msg.channel == "commentary" and not msg.recipient:
                    commentary_content = msg_text

        # Extract partial content from the parser state if the generation was truncated
        if parser.current_content:
            if parser.current_channel == "final":
                final_content = parser.current_content
            elif (
                parser.current_channel == "commentary" and not parser.current_recipient
            ):
                commentary_content = parser.current_content

        return ExtractedToolCallInformation(
            tools_called=len(tool_calls) > 0,
            tool_calls=tool_calls,
            # prefer final content over commentary content if both are present
            # commentary content is tool call preambles meant to be shown to the user
            content=final_content or commentary_content,
        )

    def extract_tool_calls_streaming(
        self,
        previous_text: str,
        current_text: str,
        delta_text: str,
        previous_token_ids: Sequence[int],
        current_token_ids: Sequence[int],
        delta_token_ids: Sequence[int],
        request: ChatCompletionRequest,
    ) -> DeltaMessage | None:
        raise NotImplementedError(
            "Not being used, manual parsing in serving_chat.py"  # noqa: E501
        )
