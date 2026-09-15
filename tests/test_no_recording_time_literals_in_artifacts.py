"""Negative test, inspired by a competing implementation's
`test_no_hardcoded_member_id`: not "does discovery emit a valid artifact",
but "does the specific input value used to RECORD that artifact end up
persisted inside it". `test_compile_locators.py` already proves the
underlying compiler function refuses to anchor a locator on dynamic row
data; this test is the stronger, whole-file version -- a JSON scan of every
artifact actually committed to this repo, so a future regression anywhere
in the compile/save path (not just the one function above) gets caught.

Scope is deliberately narrow: the recording-time literals from
`src/cua/target_app/data.py` (the member id used for the one real live
discovery run, and that member's real name/balance) must not appear
anywhere in a saved capability artifact or in discovery's own
`artifact_emitted.json` -- steps, targets, summary, goal, provenance,
runtime_matches, everything. It does NOT scan README.md/REPORT.md/
evidence/README.md, which legitimately quote `-p member_id=12345` as a
command example; see evidence/README.md's own note on that distinction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]

# The one real discovery run's actual recording-time input, and the real
# fixture data it happened to resolve to (src/cua/target_app/data.py).
# Not the mere presence of the digits "12345" as an opaque id (a template
# placeholder like "{{input.member_id}}" never contains this substring
# anyway) -- literal input/output *values* is the leak this test guards.
FORBIDDEN_LITERALS = ("12345", "99999", "Dolores Ibarra", "816000", "816_000")

ARTIFACT_FILES = sorted(ROOT.glob("artifacts/**/*.json"))
DISCOVERY_ARTIFACT_FILES = sorted(ROOT.glob("evidence/discovery-*/artifact_emitted.json"))
ALL_FILES = ARTIFACT_FILES + DISCOVERY_ARTIFACT_FILES


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_recording_time_literal_survives_into_a_committed_artifact(path: Path) -> None:
    raw = path.read_text()
    for literal in FORBIDDEN_LITERALS:
        assert literal not in raw, (
            f"{path.relative_to(ROOT)} contains the recording-time literal {literal!r} -- "
            "an artifact must only ever carry {{input.*}}/{{ctx.*}} references, never the "
            "concrete value that was typed or read while it was being discovered."
        )


def test_this_test_actually_covers_something() -> None:
    """A parametrized test with zero cases silently passes -- this fails
    loudly instead if the glob patterns above ever stop matching any real
    file (e.g. a directory rename), so the guard above can't go quiet."""
    assert ALL_FILES, "no artifact or discovery-emitted-artifact files were found to scan"


def test_forbidden_literals_are_still_the_real_fixture_data() -> None:
    """Keeps this test honest against src/cua/target_app/data.py itself --
    if the fixture data changes, this test's literals must change with it,
    not silently scan for strings that no longer mean anything."""
    from cua.target_app.data import MEMBERS

    member = MEMBERS["12345"]
    assert member["name"] == "Dolores Ibarra"
    assert member["savings_balance_minor"] == 816_000
    assert json.dumps(member)  # sanity: the fixture is still well-formed
