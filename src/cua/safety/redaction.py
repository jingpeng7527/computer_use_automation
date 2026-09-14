"""Runtime redaction -- the second of two independent mechanisms (REPORT.md
sec 6). The first is structural: artifacts bind values by reference, so a
recorded literal is absent by construction (schema/capability.py's
goal-leak guard, schema/steps.py's reference-only value_from) -- nothing to
redact is stronger than redacting. This module is the fallback for
everything that still has to be written down somewhere: a replay result,
an evidence file, a log line.

Values are masked, not hashed or omitted: `member_id=<pii:5 digits>`
rather than a hash or a blank, because a log that can't distinguish two
runs of the same capability isn't debuggable, and evidence has to explain
a run. Correlation is carried by the run id, which isn't sensitive.
"""

from __future__ import annotations

import re

from .policy import Policy


def mask(value: str, tag: str) -> str:
    kind = "digits" if value.isdigit() else "chars"
    return f"<{tag}:{len(value)} {kind}>"


def redact_value(
    value: str,
    *,
    sensitivity: str = "none",
    field_name: str | None = None,
    policy: Policy,
) -> str:
    """Mask on the field's declared sensitivity first; failing that, on its
    name; failing that, a regex pass for shapes that slipped through
    undeclared (SSN, PAN, bearer tokens, ...)."""
    if sensitivity != "none":
        return mask(value, sensitivity)
    if field_name and field_name.lower() in {
        n.lower() for n in policy.redaction.sensitive_field_names
    }:
        return mask(value, "sensitive")
    for name, pattern in policy.redaction.patterns.items():
        if re.search(pattern, value):
            return mask(value, name)
    return value


def redact_ctx_value(value: str) -> str:
    """A ctx.* value is masked UNCONDITIONALLY, regardless of any declared
    sensitivity -- it came off a live screen at replay time, so there was
    never a chance to tag it in advance (系统设计 sec 2.4)."""
    return mask(value, "ctx")


def redact_text(text: str, policy: Policy) -> str:
    """Regex fallback over free text (e.g. a FailureDetail.observed string)
    -- catches a sensitive shape appearing somewhere nothing declared."""
    redacted = text
    for name, pattern in policy.redaction.patterns.items():
        redacted = re.sub(pattern, f"<{name}>", redacted)
    return redacted
