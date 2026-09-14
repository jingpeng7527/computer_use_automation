"""The tool schema shown to the model -- OpenAI-style function specs, the
one shape both providers speak. Names match schema.steps' Action.type
literals exactly (navigate/click/type/read) so the loop needs no
translation table between "what the model called" and "what step this
becomes"; `finish` is the one tool with no Action counterpart.

The model never invents a CSS selector or types a raw description of an
element -- it's shown a `node_id` per visible element (agent/loop.py's
observation text) and always acts by node_id. This is what keeps discovery
robust on a legacy page with no accessible names: the model doesn't need
one to refer to something, it just needs the id already assigned to it.
"""

from __future__ import annotations

from typing import Any

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to an absolute URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an interactive element, identified by its node_id.",
            "parameters": {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": (
                "Type text into an input identified by its node_id. Since this value will "
                "be supplied by the caller on every future invocation (never hard-coded), "
                "give it a short snake_case param_name -- e.g. 'member_id' for an account "
                "number taken from the goal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "text": {"type": "string"},
                    "param_name": {"type": "string"},
                    "submit": {
                        "type": "boolean",
                        "description": "press Enter after typing (submits a form)",
                    },
                },
                "required": ["node_id", "text", "param_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": (
                "Read the visible text of an element identified by its node_id, and record "
                "it under output_name -- this becomes a value the caller gets back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "output_name": {"type": "string"},
                },
                "required": ["node_id", "output_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Call this once the goal is fully accomplished, or if it cannot be "
                "accomplished at all. Include every value the goal asked for in `outputs`, "
                "using the same names you gave via `read`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "outputs": {
                        "type": "object",
                        "description": "output_name -> value, from earlier `read` calls",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["success", "reason"],
            },
        },
    },
]
