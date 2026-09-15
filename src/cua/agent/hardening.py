"""The hardening pass: derives runtime_matches from OBSERVED divergence,
not a guessed list.

A happy-path discovery run never encounters "no such member" -- which
means an artifact compiled straight from it declares no business outcomes
at all, and the error taxonomy the brief asks for would be empty. The fix
is not to ask a model to speculate about failure modes (a model asked to
imagine what might go wrong produces plausible strings that never appear
on screen); it's to replay the same compiled steps -- no LLM -- with a
deliberately bad input, see exactly where and how it diverges, and read
the classification off mechanical properties of that divergence:

    a stable, fully-rendered page with no error signal   -> business_outcome
    an error surface (a "System Error" / 500-shaped page) -> hard_failure
    a dialog-shaped element present                        -> recoverable

Naming the outcome (MEMBER_NOT_FOUND vs. some other code) is the one part
a human confirms before an artifact is promoted from draft -- classification
is derived, naming is not.
"""

from __future__ import annotations

from typing import Literal

from cua.safety import Policy
from cua.schema import Capability, RecoveryAction, RuntimeMatch, TextContains, TextMatcher
from cua.surface import SurfaceAdapter, SurfaceSnapshot

from ..replay import replay

Category = Literal["business_outcome", "recoverable", "hard_failure"]


def classify_divergence(snapshot: SurfaceSnapshot, failure_kind: str, policy: Policy) -> Category:
    """Pure and mechanical, on purpose: no model in this decision either.
    `failure_kind` is the FailureKind the replay engine already assigned
    (e.g. "app_error" from an exception is an immediate hard_failure);
    everything else is read off what's actually on the page.

    The error-signal check runs BEFORE the dialog check, not after: a
    dialog-shaped element is only actually "recoverable" if nothing on
    screen also says this is a real fault. Checking dialog-shape first
    would call a dialog reading "System Error 500" recoverable -- wrong in
    the most dangerous direction, since `recoverable` triggers automatic
    retries against what is actually a hard failure. `hard_failure_signals`
    comes from `policy.error_classification`, not a hardcoded tuple, since
    different target apps phrase their error pages differently."""
    if failure_kind == "app_error":
        return "hard_failure"
    text = " ".join(n.text for n in snapshot if n.text).lower()
    if any(signal in text for signal in policy.error_classification.hard_failure_signals):
        return "hard_failure"
    if any(n.role == "dialog" for n in snapshot):
        return "recoverable"
    return "business_outcome"


def run_hardening_pass(
    capability: Capability,
    bad_params: dict[str, str],
    adapter: SurfaceAdapter,
    policy: Policy,
    *,
    run_id: str,
):
    """Replays `capability` with `bad_params` (no LLM). Returns
    (category, candidate_texts, ReplayResult): `category` is derived
    mechanically (classify_divergence); `candidate_texts` is every piece of
    text actually visible on the divergent page, for a human curator to
    pick the one that should identify this outcome -- picking it
    automatically was tried and was wrong (it defaulted to the engine's
    generic failure description, e.g. "no strategy resolved to exactly one
    element", which the target page obviously never renders and so could
    never actually fire on replay). Raises if nothing diverged -- there's
    nothing to harden if bad input still succeeds."""
    result = replay(capability, bad_params, adapter, policy, run_id=run_id)
    if result.status != "failed":
        raise ValueError(
            f"expected bad_params={bad_params!r} to diverge from the happy path, "
            f"but replay returned status={result.status!r}"
        )
    snapshot = adapter.observe()
    category = classify_divergence(snapshot, result.failure.kind, policy)
    candidate_texts = [n.text for n in snapshot if n.text]
    return category, candidate_texts, result


def build_runtime_match(
    *,
    after_step: str,
    category: Category,
    detect_text: str,
    outcome_code: str | None = None,
    recovery: RecoveryAction | None = None,
    max_retries: int = 2,
) -> RuntimeMatch:
    """Turns a classified divergence into a schema RuntimeMatch. The
    detection condition matches on the same text a human reviewer would
    point at to explain the divergence -- simple and auditable, not a
    fragile structural fingerprint."""
    return RuntimeMatch(
        id=f"{after_step}_{category}",
        category=category,
        terminal=category != "recoverable",
        detect=TextContains(text=TextMatcher(mode="contains", value=detect_text)),
        after_step=after_step,
        result_code=outcome_code,
        recovery=recovery,
        max_retries=max_retries,
    )
