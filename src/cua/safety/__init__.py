"""Safety & policy guardrails (assignment 3.4): loaded config, allowlist
enforcement, risk tiering, execution bounds, and redaction. All enforced
here, in the executor -- an artifact is data, and data does not get to
authorise its own actions (REPORT.md sec 6).
"""

from __future__ import annotations

from .allowlist import AllowlistDecision, check_allowed
from .bounds import BoundExceeded, ExecutionGuard
from .policy import ErrorClassification, ExecutionBounds, KeepAlive, Policy, load_policy
from .redaction import mask, redact_ctx_value, redact_text, redact_value
from .risk import RiskDecision, RiskTier, classify_risk, gate

__all__ = [
    "AllowlistDecision",
    "BoundExceeded",
    "ErrorClassification",
    "ExecutionBounds",
    "ExecutionGuard",
    "KeepAlive",
    "Policy",
    "RiskDecision",
    "RiskTier",
    "check_allowed",
    "classify_risk",
    "gate",
    "load_policy",
    "mask",
    "redact_ctx_value",
    "redact_text",
    "redact_value",
]
