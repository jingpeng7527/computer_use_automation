"""The LLM discovery loop: observe -> decide -> act, and compiling a
successful run into a Capability artifact. Only this package ever calls an
LLM in the whole system -- replay (a later phase) never does.
"""

from __future__ import annotations

from .compile import compile_capability
from .loop import BoundExceeded, DiscoveryTranscript, StepLog, run_discovery
from .providers import FallbackProvider, GeminiProvider, GroqProvider, LLMProvider, ToolCall

__all__ = [
    "BoundExceeded",
    "DiscoveryTranscript",
    "FallbackProvider",
    "GeminiProvider",
    "GroqProvider",
    "LLMProvider",
    "StepLog",
    "ToolCall",
    "compile_capability",
    "run_discovery",
]
