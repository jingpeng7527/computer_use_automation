"""Full live demo of escalation + handoff, producing replay-r004-equivalent
evidence: a run gets stuck, an operator claims it, fixes the SAME live
session by hand, releases it, and the automation resumes and finishes.

No threading here on purpose: Playwright's sync API and a single sqlite3
connection can each only be touched from the thread that created them,
which a first version of this script learned the hard way (a
`greenlet.error: Cannot switch to a different thread` and a
`sqlite3.ProgrammingError` in the same run). Everything below runs
sequentially on one thread instead -- which is also a more honest match
for what actually happens: one browser, one session, handled one step at
a time, exactly like a real operator taking over a real window would see.

Honesty note on what's simulated: cua ops claim/release (tested for real,
cross-process, in the CLI itself) IS the real coordination mechanism. The
"operator fixes the page by hand" step here is real code acting on the
SAME WebAdapter/browser session the stuck run was using -- there's just no
human with a mouse in this environment to do it, so this script does the
equivalent fix programmatically, on the same adapter, then hands control
back through the same real broker a person would.

Requires `cua serve-target` running in another terminal first.
"""

from __future__ import annotations

import time
from pathlib import Path

from cua.escalation import ControlBroker
from cua.replay import replay
from cua.safety import load_policy
from cua.schema import Capability, Navigate
from cua.surface import WebAdapter

ARTIFACT = "artifacts/acme_core.lookup_savings_balance/2.json"
BASE_URL = "http://127.0.0.1:8800"
RUN_ID = f"demo-handoff-{int(time.time())}"


def _faulted_capability(capability: Capability) -> Capability:
    steps = list(capability.steps)
    faulted_action = steps[0].action.model_copy(
        update={"url_template": f"{BASE_URL}/members/search?inject=500"}
    )
    steps[0] = steps[0].model_copy(update={"action": faulted_action})
    return capability.model_copy(update={"steps": steps})


def main() -> None:
    capability = Capability.model_validate_json(Path(ARTIFACT).read_text())
    policy = load_policy()
    adapter = WebAdapter(headless=False)
    broker = ControlBroker()
    evidence_dir = Path("evidence") / RUN_ID
    evidence_dir.mkdir(parents=True, exist_ok=True)

    print(f"[automation] run {RUN_ID!r}: about to hit an injected 500 on step one...")
    stuck_result = replay(
        _faulted_capability(capability),
        {"member_id": "12345"},
        adapter,
        policy,
        run_id=RUN_ID,
        evidence_dir=str(evidence_dir),
        broker=broker,
        escalation_poll_s=0.2,
        escalation_max_wait_s=0.5,  # nobody's claimed it yet -- give up fast, we'll drive it below
    )
    print(f"[automation] stuck: status={stuck_result.status}")
    (evidence_dir / "result_before_escalation.json").write_text(stuck_result.model_dump_json(indent=2))

    row = broker.get_state(RUN_ID)
    print(f"[broker] state: {row.state}, reason={row.reason!r}")
    assert row.state == "PAUSED"

    print("[operator] claiming control of the SAME run_id (same browser session)...")
    assert broker.claim(RUN_ID, holder="demo-operator", lease_seconds=60)
    print(f"[broker] state: {broker.get_state(RUN_ID).state}, holder={broker.get_state(RUN_ID).holder}")

    print("[operator] fixing the live session by hand -- re-navigating past the injected fault...")
    adapter.act(Navigate(url_template=""), value=f"{BASE_URL}/members/search")
    print(f"[operator] browser is now at: {adapter.location()}")

    print("[operator] releasing control back...")
    assert broker.release(RUN_ID, holder="demo-operator")
    print(f"[broker] state: {broker.get_state(RUN_ID).state}")

    print("\n[automation] resuming on the SAME browser/session, capability now un-faulted...")
    final_result = replay(
        capability,  # the real, un-faulted artifact -- the fix already happened live in the browser
        {"member_id": "12345"},
        adapter,
        policy,
        run_id=RUN_ID,
        evidence_dir=str(evidence_dir),
        broker=broker,
    )
    print(f"[automation] final status: {final_result.status}")
    if final_result.status == "success":
        print(f"[automation] outputs: {final_result.outputs}")
    (evidence_dir / "result_after_resume.json").write_text(final_result.model_dump_json(indent=2))
    print(f"\nevidence written to {evidence_dir}/")

    adapter.close()
    broker.close()


if __name__ == "__main__":
    main()
