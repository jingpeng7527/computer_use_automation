"""Safety & policy guardrails (assignment 3.4): loaded config, allowlist
enforcement, and risk tiering. All enforced here, in the executor -- an
artifact is data, and data does not get to authorise its own actions
(REPORT.md sec 6). Redaction and execution bounds are a later phase.
"""

from __future__ import annotations

from .allowlist import AllowlistDecision, check_allowed
from .policy import Policy, load_policy
from .risk import RiskDecision, RiskTier, classify_risk, gate

__all__ = [
    "AllowlistDecision",
    "Policy",
    "RiskDecision",
    "RiskTier",
    "check_allowed",
    "classify_risk",
    "gate",
    "load_policy",
]
