"""Gemini as the primary discovery-time provider.

Perception is the accessibility-tree-derived SurfaceSnapshot, rendered as
text (agent/loop.py) -- never a screenshot -- so nothing here needs vision.
What's asked of the model is tool calling over a text observation, which
is why a general-purpose model is the right fit rather than a purpose-built
computer-use/vision model answering a question this design doesn't ask.
"""

from __future__ import annotations

import os
from typing import Any

from google import genai
from google.genai import types

from .base import ToolCall


class GeminiProvider:
    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.environ.get("CUA_LLM_MODEL", "gemini-3.6-flash")
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        self._client = genai.Client(api_key=key)

    def decide(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> ToolCall:
        system = next((m["content"] for m in messages if m["role"] == "system"), None)
        contents = [
            types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[types.Part(text=m["content"])],
            )
            for m in messages
            if m["role"] != "system"
        ]
        declarations = [
            types.FunctionDeclaration(
                name=t["function"]["name"],
                description=t["function"]["description"],
                parameters_json_schema=t["function"]["parameters"],
            )
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=declarations)],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY
                )
            ),
        )
        response = self._client.models.generate_content(
            model=self.model, contents=contents, config=config
        )
        calls = response.function_calls
        if not calls:
            raise RuntimeError(f"model returned no tool call: {response.text!r}")
        call = calls[0]
        return ToolCall(name=call.name, args=dict(call.args or {}))
