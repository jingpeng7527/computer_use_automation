"""Groq as the secondary fallback -- used only when the primary (Gemini)
call errors, per the decision to keep Gemini primary rather than switch
providers outright. Groq's API is a documented drop-in for the OpenAI SDK
via `base_url`, so no Groq-specific SDK is needed and `tools` passes
through unchanged (it's already the OpenAI tool-call shape).
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

from .base import ToolCall


class GroqProvider:
    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.environ.get("CUA_GROQ_MODEL", "openai/gpt-oss-120b")
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set")
        self._client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=key)

    def decide(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> ToolCall:
        response = self._client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, tool_choice="required"
        )
        message = response.choices[0].message
        if not message.tool_calls:
            raise RuntimeError(f"model returned no tool call: {message.content!r}")
        call = message.tool_calls[0]
        return ToolCall(name=call.function.name, args=json.loads(call.function.arguments))
