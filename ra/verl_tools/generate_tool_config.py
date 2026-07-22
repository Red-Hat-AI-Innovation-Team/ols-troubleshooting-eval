"""Generate verl tool_config.yaml from raw_tool_defs.json.

Reads ra/raw_tool_defs.json and produces tool_config.yaml with one entry per
tool, all pointing to OLSTroubleshootingTool but with distinct tool_schema.
Also adds tools present in mock_tools.TOOLS but absent from raw_tool_defs.json.
"""

import json
import sys
from pathlib import Path

import yaml

# Ensure ra/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CLASS_NAME = "verl_tools.ols_tool.OLSTroubleshootingTool"
STRIP_PARAMS = {"context"}  # not supported by the mock tools


def _clean_parameters(params: dict) -> dict:
    """Remove the ``context`` param and rebuild required list."""
    props = {k: v for k, v in params.get("properties", {}).items() if k not in STRIP_PARAMS}
    required = [r for r in params.get("required", []) if r not in STRIP_PARAMS]
    cleaned: dict = {"type": "object", "properties": props, "required": required}
    return cleaned


def generate():
    raw_path = Path(__file__).resolve().parent.parent / "raw_tool_defs.json"
    raw = json.loads(raw_path.read_text())

    # Collect all tool names from raw_tool_defs.json
    tools_config = []
    seen_names: set[str] = set()

    for _server, section in raw.items():
        if not isinstance(section, dict) or "tools" not in section:
            continue
        for t in section["tools"]:
            fn = t["function"]
            name = fn["name"]
            seen_names.add(name)

            params = _clean_parameters(fn.get("parameters", {"type": "object", "properties": {}}))

            entry = {
                "class_name": CLASS_NAME,
                "config": {"type": "native"},
                "tool_schema": {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": fn.get("description", ""),
                        "parameters": params,
                    },
                },
            }
            tools_config.append(entry)

    # Add tools in mock_tools.TOOLS but not in raw_tool_defs.json
    from mock_tools import TOOLS, EXTRA_TOOL_DEFS

    for extra in EXTRA_TOOL_DEFS:
        if extra.name not in seen_names:
            entry = {
                "class_name": CLASS_NAME,
                "config": {"type": "native"},
                "tool_schema": {
                    "type": "function",
                    "function": {
                        "name": extra.name,
                        "description": extra.description,
                        "parameters": extra.parameters,
                    },
                },
            }
            tools_config.append(entry)
            seen_names.add(extra.name)

    # Check for any remaining tools in TOOLS dict not yet covered
    for tool_name in TOOLS:
        if tool_name not in seen_names:
            entry = {
                "class_name": CLASS_NAME,
                "config": {"type": "native"},
                "tool_schema": {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": f"Execute {tool_name} tool",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                },
            }
            tools_config.append(entry)
            seen_names.add(tool_name)

    config = {"tools": tools_config}

    out_path = Path(__file__).resolve().parent / "tool_config.yaml"
    out_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    print(f"Written {len(tools_config)} tool entries to {out_path}")


if __name__ == "__main__":
    generate()
