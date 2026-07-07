"""Agent dataclass: generic LLM agent loop with tool calling."""

import json
from dataclasses import dataclass, field
from typing import Callable

from llm.base import LLMClient
from llm.types import Message, ToolDef, ToolResult

# tool_handler signature: (tool_name: str, params: dict) -> str
ToolHandler = Callable[[str, dict], str]


@dataclass
class Agent:
    system_prompt: str
    model: str
    tool_defs: list[ToolDef]
    tool_handler: ToolHandler
    client: LLMClient
    max_turns: int = 20
    thinking_budget: int = 10_000
    max_tokens: int = 64_000
    messages: list[Message] = field(default_factory=list)

    def run(self, query: str) -> str:
        """Run the agent loop for a user query. Returns the final text answer."""
        self.messages.append(Message(role="user", content=query))

        print(f"User: {query}\n")
        print("=" * 60)

        for turn in range(self.max_turns):
            print(f"\n--- Turn {turn + 1} ---")

            response = self.client.chat(
                model=self.model,
                messages=self.messages,
                tools=self.tool_defs,
                max_tokens=self.max_tokens,
                system=self.system_prompt,
                thinking_budget=self.thinking_budget,
            )

            for tc in response.tool_calls:
                params_str = json.dumps(tc.arguments, separators=(",", ":")) if tc.arguments else "{}"
                print(f"  -> {tc.name}({params_str})")

            if not response.tool_calls:
                final_answer = response.content or ""
                self.messages.append(Message(role="assistant", content=response.content))
                return final_answer

            # Store assistant message with tool calls
            self.messages.append(Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            ))

            # Execute tools
            tool_results: list[ToolResult] = []
            for tc in response.tool_calls:
                try:
                    result = self.tool_handler(tc.name, tc.arguments or {})
                except Exception as e:
                    result = f"Error executing {tc.name}: {type(e).__name__}: {e}"
                tool_results.append(ToolResult(tool_call_id=tc.id, content=result))

            self.messages.append(Message(role="tool", tool_results=tool_results))

        default_ret = "[Agent hit max turns without producing a final answer]"
        self.messages.append(Message(role='assistant', content=default_ret))
        return default_ret
