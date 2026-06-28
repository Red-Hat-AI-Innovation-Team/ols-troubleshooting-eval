"""Agent dataclass: generic LLM agent loop with tool calling."""

import json
from dataclasses import dataclass, field
from typing import Callable

from anthropic import AnthropicVertex

# tool_handler signature: (tool_name: str, params: dict) -> str
ToolHandler = Callable[[str, dict], str]


@dataclass
class Agent:
    system_prompt: str
    model: str
    tool_defs: list[dict]
    tool_handler: ToolHandler
    client: AnthropicVertex
    max_turns: int = 20
    thinking_budget: int = 10_000
    max_tokens: int = 16_000
    messages: list[dict] = field(default_factory=list)

    def run(self, query: str) -> str:
        """Run the agent loop for a user query. Returns the final text answer."""
        self.messages.append({"role": "user", "content": query})

        print(f"User: {query}\n")
        print("=" * 60)

        for turn in range(self.max_turns):
            print(f"\n--- Turn {turn + 1} ---")

            with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                thinking={"type": "enabled", "budget_tokens": self.thinking_budget},
                system=self.system_prompt,
                tools=self.tool_defs,
                messages=self.messages,
            ) as stream:
                response = stream.get_final_message()

            tool_uses: list = []
            text_parts: list[str] = []

            for block in response.content:
                if block.type == "tool_use":
                    tool_uses.append(block)
                elif block.type == "text":
                    text_parts.append(block.text)

            for tu in tool_uses:
                params_str = json.dumps(tu.input, separators=(",", ":")) if tu.input else "{}"
                print(f"  -> {tu.name}({params_str})")

            if not tool_uses:
                final_answer = "\n".join(text_parts)
                self.messages.append({"role": "assistant", "content": response.content})
                return final_answer

            self.messages.append({"role": "assistant", "content": response.content})

            tool_results: list[dict] = []
            for tu in tool_uses:
                try:
                    result = self.tool_handler(tu.name, tu.input or {})
                except Exception as e:
                    result = f"Error executing {tu.name}: {type(e).__name__}: {e}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result,
                })

            self.messages.append({"role": "user", "content": tool_results})

        return "[Agent hit max turns without producing a final answer]"
