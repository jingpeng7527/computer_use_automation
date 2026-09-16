"""Shared building blocks: text matching, money, and the condition vocabulary.

Conditions are the one predicate language used everywhere a "did we actually
reach the expected state" question is asked: step checkpoints, the
capability's overall success condition, and runtime-match detection
(business outcomes, recoverable conditions, hard failures all share it).
One vocabulary for all three keeps the replay engine simple -- it only ever
evaluates Condition objects, never bespoke per-feature logic.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class TextMatcher(BaseModel):
    mode: Literal["equals", "contains", "regex", "not_contains"] = "contains"
    value: str
    ignore_case: bool = True


class Money(BaseModel):
    """The only representation for currency anywhere in this system. Never a
    float: binary floating point cannot represent decimal currency exactly,
    and this is regulated financial data (REPORT.md sec 2)."""

    amount_minor: int
    currency: str


# A value produced by a Read step or returned in a result. Typed, not
# downgraded to a string: a money output stays a Money object all the way
# through StepResult/BusinessOutcomeResult/ReplayResult (schema/result.py).
# No `float` arm: ParamType has no "float" variant and _typed_value()
# (replay/engine.py) never produces one -- money is Decimal-backed Money
# specifically to avoid float's precision loss, so a stray float branch
# here would have been an unreachable, misleading escape hatch back into
# the imprecision the Money type exists to rule out.
OutputValue = Money | bool | int | str


_REF_RE = re.compile(r"^\{\{(input|ctx)\.([A-Za-z_][A-Za-z0-9_]*)\}\}$")
_INTO_RE = re.compile(r"^(ctx\.[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*)$")


def parse_ref(template: str) -> tuple[str, str]:
    """Parse a `"{{input.x}}"` / `"{{ctx.x}}"` value binding and return
    (namespace, name). Values are bound by reference ONLY, never by literal
    (REPORT.md sec 2: "the concrete member id used during recording is
    structurally absent from the file") -- this is what enforces that at
    the syntax level, on every TypeText/Select.value_from in steps.py.
    Raises ValueError if `template` isn't exactly one such reference.
    """
    m = _REF_RE.match(template)
    if not m:
        raise ValueError(
            f"{template!r} is not a valid reference; expected \"{{{{input.<name>}}}}\" "
            'or "{{ctx.<name>}}" -- a step may bind a value by reference only, never a literal'
        )
    return m.group(1), m.group(2)


def validate_into(value: str) -> str:
    """Validate a Read.into target: either `"ctx.<name>"` (feeds a later
    step, never leaves this run) or a bare identifier naming a declared
    OutputSpec. Used as a field_validator on steps.Read.into."""
    if not _INTO_RE.match(value):
        raise ValueError(f'{value!r} must be "ctx.<name>" or a bare output name')
    return value


class TextContains(BaseModel):
    kind: Literal["text_contains"] = "text_contains"
    text: TextMatcher


class RoleName(BaseModel):
    """An element with this accessibility role and accessible name is
    present (and, if `visible`, currently visible)."""

    kind: Literal["role_name"] = "role_name"
    role: str
    name: str
    visible: bool = True


class UrlMatches(BaseModel):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str  # glob-ish, e.g. "*/members/*/detail"


class ValueEquals(BaseModel):
    """A form control's current value equals an expected, possibly templated string."""

    kind: Literal["value_equals"] = "value_equals"
    role: str
    name: str
    expected_template: str


Condition = Annotated[
    TextContains | RoleName | UrlMatches | ValueEquals,
    Field(discriminator="kind"),
]
# A NamedPredicate escape hatch ("no_error_banner", "network_idle", ...) for
# an engine-known check that doesn't reduce to text or a role/name was here
# and deliberately removed: evaluate_condition() never implemented it and
# unconditionally returned False for it, which meant any Condition that
# used one made its enclosing Checkpoint permanently unsatisfiable --
# structurally valid, silently unpassable. Add it back to the union only
# together with a real evaluate_condition() branch, not ahead of one.


class Checkpoint(BaseModel):
    """All conditions must hold (AND). Proves a step -- or the whole
    capability -- actually reached the state it claims, rather than
    assuming the previous action worked."""

    description: str
    all_of: list[Condition] = Field(min_length=1)
    timeout_ms: int = 10_000
