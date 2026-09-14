"""Risk tiering -- assigned by the executor from the action type and target,
never accepted from the artifact's own Step.risk_level hint (系统设计 P5:
a mislabelled step must not be able to downgrade its own risk).
IRREVERSIBLE is refused by default; policy.irreversible_policy can route it
to human authorisation instead (sec 5) but never to silent unattended
execution -- the asymmetry is deliberate (REPORT.md sec 6): a blocked
legitimate action costs a minute, an unauthorised irreversible one in a
core banking system does not undo by retrying.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Literal

from cua.surface import ResolutionResult

from .policy import Policy

RiskTier = Literal["SAFE_READ", "REVERSIBLE_WRITE", "IRREVERSIBLE"]


def classify_risk(
    action_type: str,
    policy: Policy,
    *,
    resolution: ResolutionResult | None = None,
    destination: str | None = None,
) -> RiskTier:
    name = None
    if resolution is not None and resolution.node is not None:
        name = resolution.node.name or resolution.node.text

    if name and any(
        phrase.lower() in name.lower() for phrase in policy.risk_tiers.irreversible.name_contains
    ):
        return "IRREVERSIBLE"
    if destination and any(
        fnmatch.fnmatch(destination, pattern)
        for pattern in policy.risk_tiers.irreversible.path_patterns
    ):
        return "IRREVERSIBLE"
    if action_type in policy.risk_tiers.safe_read.action_types:
        return "SAFE_READ"
    return "REVERSIBLE_WRITE"


@dataclass
class RiskDecision:
    tier: RiskTier
    allowed_unattended: bool
    reason: str = ""
    # None when allowed_unattended; otherwise which of the two IRREVERSIBLE
    # postures produced the refusal. The caller (replay's executor) branches
    # on this, not on `reason` text, to route "refuse" to a hard FAILURE and
    # "require_confirm" to escalation -- conflating the two would mean a
    # policy of "refuse" still asks a human to authorise the very action the
    # policy says must never run unattended OR by hand-off.
    escalate: bool = False


def gate(
    action_type: str,
    policy: Policy,
    *,
    resolution: ResolutionResult | None = None,
    destination: str | None = None,
) -> RiskDecision:
    tier = classify_risk(action_type, policy, resolution=resolution, destination=destination)
    if tier != "IRREVERSIBLE":
        return RiskDecision(tier, True)
    if policy.irreversible_policy == "require_confirm":
        return RiskDecision(
            tier, False, "irreversible action requires human authorisation", escalate=True
        )
    return RiskDecision(tier, False, "irreversible actions are refused by policy", escalate=False)
