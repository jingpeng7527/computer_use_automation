"""Member fixtures for the mock legacy servicing console.

Phase B shipped a single happy-path member, 12345, with the not-found /
frozen / permission-denied / injected-error fixtures noted as landing
"once a hardening pass exists that needs them to derive runtime_matches
from observed divergence rather than a guessed list." The hardening pass
landed in Phase G and shipped not-found (search-time) and injected-error
(?inject=500); REPORT.md's write-up went on to describe frozen and
permission-denied as if they existed too, but nothing ever added the
member records -- a real gap between the design doc and this file, closed
here: 99001 and 99002 exist specifically so `cua harden -p
member_id=99001` (etc.) can derive ACCOUNT_FROZEN and PERMISSION_DENIED
the same observed-divergence way 12345/99999 already derives
MEMBER_NOT_FOUND, rather than hand-authoring the runtime_match.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class Member(TypedDict):
    name: str
    savings_balance_minor: int
    currency: str
    # Absent (the default) means an ordinary, viewable account. detail.html
    # branches on this to withhold the balance row entirely -- the
    # divergence a "frozen" or "permission-denied" lookup produces is that
    # step s4's Target (anchored on the "Savings Balance" label) resolves to
    # nothing, the same target_not_found mechanism 99999 already exercises
    # for MEMBER_NOT_FOUND, just at a later step.
    status: NotRequired[Literal["frozen", "restricted"]]


MEMBERS: dict[str, Member] = {
    "12345": {
        "name": "Dolores Ibarra",
        "savings_balance_minor": 816_000,
        "currency": "USD",
    },
    "99001": {
        "name": "Marcus Whit",
        "savings_balance_minor": 250_000,
        "currency": "USD",
        "status": "frozen",
    },
    "99002": {
        "name": "Priya Nathan",
        "savings_balance_minor": 4_200_000,
        "currency": "USD",
        "status": "restricted",
    },
}
