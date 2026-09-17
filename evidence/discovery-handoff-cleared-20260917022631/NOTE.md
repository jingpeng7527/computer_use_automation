Real `run_discovery()`, a real SQLite `ControlBroker` across a real thread boundary
(mirroring the two real OS processes `cua discover` and `cua ops` actually are), real
`claim()`/`release(resolution="cleared_obstacle")`, real `compile_capability()`, a real
saved artifact, and a real `cua approve` invocation refusing it (see
`approve_refusal_without_validation.txt`) -- only the `LLMProvider` and `SurfaceAdapter`
are scripted, same technique as `tests/test_discovery_handoff.py`.

Why scripted: reliably forcing a real Gemini/Groq-driven discovery run into a genuine
"no progress for N consecutive steps" stall, on demand, isn't something a live model call
can be made to do deterministically -- and doing it by accident would mean burning real
API quota hoping for a stall instead of demonstrating one. The mechanism exercised here is
100% real: `guard.check_progress()` raising `BoundExceeded`, `_handle_discovery_stall()`
raising a real discovery-phase intervention through the real broker (`intervention.json`
in this directory is its genuine output), the real poll-for-`RESUMING` loop, and
`guard.reset_progress()` handing control back to the (scripted) LLM with a fresh
observation once the operator's fix is in place.

`intervention.json`'s `screenshot_ref` names a path with no file behind it -- this
scripted adapter's `screenshot()` is a no-op, unlike the real `WebAdapter`.

`run_meta.json` records the full transcript summary, including `interventions[0]`
(trigger/resolution/operator_note/before_url/after_url) and the compiled artifact's
`discovery_handoffs` count. `artifact_emitted.json` is the actual compiled `Capability`
-- note `provenance.discovery_handoffs == 1` and `status == "draft"`.

`approve_refusal_without_validation.txt` is the real, unmodified `cua approve` CLI
refusing to promote this artifact without a `--validation-run`: this part needed no
scripting at all, since `approve()` only ever inspects the artifact file on disk, never
touches a browser. Running `cua validate` against this specific artifact wouldn't
demonstrate anything real, though: its steps target node_ids from the scripted adapter's
fake page, which don't correspond to anything a real `WebAdapter` could resolve --
`cua validate`'s own replay-from-a-fresh-session logic is exercised for real by the
existing not_approved/recovery_refused/session_lost evidence and by
`tests/test_validate_approve_gate.py`, just not chained onto this particular artifact.
