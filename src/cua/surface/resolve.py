"""Pure functions over a SurfaceSnapshot -- no Playwright import anywhere in
this file, which is what makes locator resolution unit-testable without a
browser (系统设计.md sec 1.7 scopes exactly this as one of the four things
worth testing).

Resolution is ranked, first-unique-match-wins, exactly as schema/locator.py
documents: strategies are tried in layer order, the first one that matches
EXACTLY ONE node wins, and a strategy matching zero or more than one node is
skipped -- ambiguity is treated as non-resolution, not disambiguated by
picking one. `evaluate_condition` is the same idea applied to the schema's
Condition vocabulary, and is reused as-is by the replay engine (a later
phase) for checkpoint and runtime_match detection -- one evaluator, so
"did we reach the expected state" is answered the same way everywhere.
"""

from __future__ import annotations

import fnmatch
import re

from cua.schema import (
    BboxStrategy,
    Condition,
    CssStrategy,
    LabelAnchorStrategy,
    NamedPredicate,
    RoleName,
    RoleNameStrategy,
    Target,
    TextContains,
    TextMatcher,
    UrlMatches,
    ValueEquals,
)

from .protocol import InteractiveNode, Location, LocatorAttempt, ResolutionResult, SurfaceSnapshot


def _text_matches(haystack: str, matcher: TextMatcher) -> bool:
    h = haystack.lower() if matcher.ignore_case else haystack
    v = matcher.value.lower() if matcher.ignore_case else matcher.value
    if matcher.mode == "equals":
        return h == v
    if matcher.mode == "contains":
        return v in h
    if matcher.mode == "not_contains":
        return v not in h
    if matcher.mode == "regex":
        flags = re.IGNORECASE if matcher.ignore_case else 0
        return re.search(matcher.value, haystack, flags) is not None
    return False


# ---- per-layer strategy matchers: each returns every node that qualifies; --
# ---- resolve_against_snapshot() is the one place that decides what to do --
# ---- with the count (0 / 1 / many). --------------------------------------


def _match_role_name(strategy: RoleNameStrategy, snapshot: SurfaceSnapshot) -> list[InteractiveNode]:
    return [n for n in snapshot if n.role == strategy.role and (n.name or "") == strategy.name]


def _row_of(anchor: InteractiveNode, snapshot: SurfaceSnapshot) -> list[InteractiveNode]:
    """Every other node sharing the anchor's visual row, left to right. This
    is a geometric stand-in for "same <tr>" -- it works whether or not the
    page actually used a <table>, which matters once a legacy screen turns
    out to fake tabular layout with plain <div>s."""
    top, bottom = anchor.bbox.y, anchor.bbox.y + anchor.bbox.h
    row = [n for n in snapshot if n is not anchor and n.bbox.vertical_overlap(top, bottom)]
    return sorted(row, key=lambda n: n.bbox.x)


def _match_label_anchor(
    strategy: LabelAnchorStrategy, snapshot: SurfaceSnapshot
) -> list[InteractiveNode]:
    matcher = TextMatcher(mode="contains", value=strategy.label)
    anchors = [n for n in snapshot if n.text and _text_matches(n.text, matcher)]
    if len(anchors) != 1:
        return []  # ambiguous or missing anchor text -> this layer doesn't resolve
    anchor = anchors[0]

    if strategy.relation == "following_field":
        idx = snapshot.index(anchor)
        candidates = [n for n in snapshot[idx + 1 :] if n.interactive]
    else:
        row = [n for n in _row_of(anchor, snapshot) if n.bbox.x > anchor.bbox.x]
        candidates = [n for n in row if n.interactive] if strategy.relation == "same_row_input" else row

    if strategy.nth >= len(candidates):
        return []
    return [candidates[strategy.nth]]


def _match_css(strategy: CssStrategy, snapshot: SurfaceSnapshot) -> list[InteractiveNode]:
    """A hand-rolled subset covering #id, .class, and tag.class -- what a
    stable, human-authored selector on a legacy page actually looks like.
    Not a general CSS engine; layer 3 is a web-only fallback already, and a
    real WebAdapter build could delegate this to Playwright's own selector
    engine instead of reimplementing it."""
    sel = strategy.selector.strip()
    if sel.startswith("#"):
        return [n for n in snapshot if n.attrs.get("id") == sel[1:]]
    if sel.startswith("."):
        cls = sel[1:]
        return [n for n in snapshot if cls in n.attrs.get("class", "").split()]
    if "." in sel:
        tag, cls = sel.split(".", 1)
        return [n for n in snapshot if n.tag == tag and cls in n.attrs.get("class", "").split()]
    return [n for n in snapshot if n.tag == sel]


def _match_bbox(strategy: BboxStrategy, snapshot: SurfaceSnapshot) -> list[InteractiveNode]:
    cx, cy = strategy.x + strategy.w / 2, strategy.y + strategy.h / 2
    return [n for n in snapshot if n.bbox.contains_point(cx, cy)]


_MATCHERS = {
    "role_name": _match_role_name,
    "label_anchor": _match_label_anchor,
    "css": _match_css,
    "bbox": _match_bbox,
}


def resolve_against_snapshot(target: Target, snapshot: SurfaceSnapshot) -> ResolutionResult:
    attempts: list[LocatorAttempt] = []
    for strategy in target.strategies:
        matches = _MATCHERS[strategy.type](strategy, snapshot)
        attempts.append(
            LocatorAttempt(
                layer=strategy.layer,
                kind=strategy.type,
                match_count=len(matches),
                matched=len(matches) == 1,
            )
        )
        if len(matches) == 1:
            return ResolutionResult(
                resolved=True, layer=strategy.layer, node=matches[0], attempts=attempts
            )
    return ResolutionResult(resolved=False, attempts=attempts)


def evaluate_condition(
    condition: Condition, snapshot: SurfaceSnapshot, location: Location | None = None
) -> bool:
    """The single evaluator behind a step's checkpoint, a WaitSpec.until,
    and runtime_match detection -- one vocabulary, one place that reads it,
    so "did we reach the expected state" never has a second implementation
    to drift out of sync with the first."""
    if isinstance(condition, TextContains):
        combined = " ".join(n.text for n in snapshot if n.text)
        return _text_matches(combined, condition.text)
    if isinstance(condition, RoleName):
        matches = [n for n in snapshot if n.role == condition.role and (n.name or "") == condition.name]
        return len(matches) >= 1
    if isinstance(condition, UrlMatches):
        if location is None:
            return False
        full = f"{location.origin}{location.path}"
        return fnmatch.fnmatch(location.path, condition.pattern) or fnmatch.fnmatch(
            full, condition.pattern
        )
    if isinstance(condition, ValueEquals):
        # `expected_template` is assumed already resolved by the caller --
        # evaluate_condition, like the adapter's act(), never touches
        # {{input.x}} / {{ctx.x}} references itself.
        matches = [n for n in snapshot if n.role == condition.role and (n.name or "") == condition.name]
        return len(matches) == 1 and (matches[0].value or "") == condition.expected_template
    if isinstance(condition, NamedPredicate):
        return False  # engine-specific predicates (e.g. "network_idle") aren't wired up yet
    return False
