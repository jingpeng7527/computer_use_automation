"""Member fixtures for the mock legacy servicing console.

Phase B (happy path only): a single member, 12345. The not-found / frozen /
permission-denied / injected-error fixtures land in a later phase (系统设计
sec 4.5), once a hardening pass exists that needs them to derive
runtime_matches from *observed* divergence rather than a guessed list.
"""

from __future__ import annotations

from typing import TypedDict


class Member(TypedDict):
    name: str
    savings_balance_minor: int
    currency: str


MEMBERS: dict[str, Member] = {
    "12345": {
        "name": "Dolores Ibarra",
        "savings_balance_minor": 816_000,
        "currency": "USD",
    },
}
