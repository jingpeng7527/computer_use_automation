"""LLMProvider: one structured decision per step, via tool calling, never
free-form text. `decide()` takes an OpenAI-style message list and tool
spec list -- a de facto standard shape both Gemini's and Groq's SDKs can be
adapted to -- so the loop itself never touches a provider-specific type.
Swapping (or adding) a provider is a new class implementing `decide()`.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any]


class LLMProvider(Protocol):
    model: str

    def decide(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> ToolCall: ...


class FallbackProvider:
    """Gemini stays primary; Groq is a secondary fallback used only if the
    primary call raises (error, rate limit, timeout) -- not a full
    provider swap. Chosen over picking one exclusively so a single
    provider hiccup doesn't fail an entire discovery run."""

    def __init__(self, primary: LLMProvider, secondary: LLMProvider | None = None) -> None:
        self.primary = primary
        self.secondary = secondary
        # Which provider actually answered the most recent decide() call --
        # evidence (run_meta/provenance) reads this per step so it never
        # claims the configured primary reasoned when a live quota/outage
        # meant the fallback did the work instead. Set before decide()
        # returns, so it's always accurate for the call that just happened.
        self.last_used_model: str = primary.model

    def decide(self, messages: list[dict[str, str]], tools: list[dict[str, Any]]) -> ToolCall:
        try:
            call = self.primary.decide(messages, tools)
            self.last_used_model = self.primary.model
            return call
        except Exception:
            if self.secondary is None:
                raise
            call = self.secondary.decide(messages, tools)
            self.last_used_model = self.secondary.model
            return call
