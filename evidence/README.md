# Evidence index

Every run below is a real execution of the real code -- a live LLM call for discovery, the actual
`replay()` engine, the actual `Capability`/`RuntimeMatch` schema, a real SQLite control-transfer
broker for every handoff run. Nothing here is hand-written output. Most rows also drive a real
headed Playwright browser against the real local FastAPI target app; reproduce those with the
target app running (`cua serve-target`) and the command shown. Five rows (marked below) instead
use a scripted `LLMProvider`/`SurfaceAdapter` -- same technique this project's own unit tests use
-- because the mock target app has no fixture that can trigger that specific condition live
(documented in each row and in that run's own `NOTE.md`). In every one of those five, only the
*surface being observed* (and, for the two discovery-handoff rows, the model) is scripted; the
engine, schema, and broker are the real code, unmodified.

| Directory | Command | Result |
| --- | --- | --- |
| `discovery-20260915043330/` | `cua discover --goal "look up member 12345 and read their current savings balance" --target http://127.0.0.1:8800/members/search --name lookup_savings_balance` | Live discovery, 6 real steps, 6.5s. Emits `acme_core.lookup_savings_balance` v1 (status `draft`). Gemini's free-tier daily quota was exhausted at the time, so `FallbackProvider` fell through to Groq mid-run -- `run_meta.json`'s `model` field (`"openai/gpt-oss-120b -> gemini-3.6-flash"`) and each step's `provider_model` say so honestly, rather than crediting the configured primary for a run it didn't fully do. |
| `replay-20260915044628/` | `cua approve --artifact artifacts/acme_core.lookup_savings_balance/1.json`, then `cua replay --artifact artifacts/acme_core.lookup_savings_balance/1.json -p member_id=12345` | `SUCCESS`. `outputs.savings_balance` is a typed `Money` object (`{"amount_minor": 816000, "currency": "USD"}`), not a raw string. |
| `replay-20260915044639/` | `cua harden ... --outcome-code MEMBER_NOT_FOUND` (emits v2, status `draft`), `cua approve --artifact .../2.json`, then `cua replay --artifact .../2.json -p member_id=99999` | `BUSINESS_OUTCOME`, code `MEMBER_NOT_FOUND`, detected after step `s2`. Not a crash: the artifact declares this outcome and the engine matched it against the observed page. |
| `replay-20260916011636/` | `cua harden` derived `ACCOUNT_FROZEN` against fixture member `99001` the same observed-divergence way as `MEMBER_NOT_FOUND` above (emits v3, then v4 alongside `PERMISSION_DENIED` below), `cua approve --artifact .../4.json`, then `cua replay --artifact .../4.json -p member_id=99001` | `BUSINESS_OUTCOME`, code `ACCOUNT_FROZEN`, detected after step `s4`. |
| `replay-20260916011639/` | same v4 artifact, `-p member_id=99002` (fixture member `99002`, permission-restricted) | `BUSINESS_OUTCOME`, code `PERMISSION_DENIED`, detected after step `s4`. Deriving both outcomes at the same step (`s4`) is what surfaced a real id collision in `build_runtime_match()` -- this row's `outcome.description` (`s4_business_outcome_PERMISSION_DENIED`) is the fix: an outcome-code/detect-text disambiguator, not just `f"{after_step}_{category}"`. |
| `replay-20260916011641/` | same v4 artifact, `-p member_id=12345` | `SUCCESS`, same typed `Money` output as `replay-20260915044628/` -- confirms adding the two new `runtime_matches` above didn't disturb the original happy path. |
| `replay-20260915044645/` | `cua replay --artifact artifacts/acme_core.lookup_savings_balance/2.json -p member_id=12345 --fault "inject=500" --no-handoff` | `FAILED` at step `s0`, kind `checkpoint_failed`. `s0-failure.png` is a real screenshot of the target app's "System Error 500" page. |
| `demo-handoff-1789447614/` | same fault, with handoff enabled: `cua replay ... --fault "inject=500"`, then `cua ops claim <run_id>` / fix by hand / `cua ops release <run_id>` | `result_before_escalation.json`: `FAILED`, `intervention.json` raised (screenshot + reason + empty completed-step list, since it failed on step `s0`). Operator reloads the URL without `?inject=500` in the same browser window, releases control. `result_after_resume.json`: `SUCCESS` -- the resumed run re-checked its checkpoint and continued through to the same typed balance output. |
| `replay-20260915224332/` | `cua overlay apply --base artifacts/acme_core.lookup_savings_balance/2.json --overlay overlays/acme_core.lookup_savings_balance/cu_northgate.json`, `cua approve --artifact artifacts/acme_core.lookup_savings_balance.cu_northgate/1.json`, then `cua replay --artifact artifacts/acme_core.lookup_savings_balance.cu_northgate/1.json -p member_id=12345 -p branch=main` | `SUCCESS` against the SECOND tenant (`/tenant-b/...` -- different field labels, a real HTML5-`required` branch selector the base tenant doesn't have, a differently-classed detail control, an abbreviated balance label). Same typed `Money` output shape as the base tenant's run. |
| `replay-20260915224345/` | same resolved tenant artifact, `-p member_id=99999 -p branch=main` | `BUSINESS_OUTCOME / MEMBER_NOT_FOUND` on the second tenant, with zero outcome-detection override in the overlay -- the base capability's detection text happens to match this tenant's build unchanged. |
| `replay-20260915230202/` | `cua replay --artifact .../2.json -p member_id=12345 --fault "inject=500"`, then `cua ops claim`, `cua ops release --note "checked the page, fault still present (no real fix applied in this live sanity check)"` -- deliberately WITHOUT fixing anything | `FAILED` on resume (the fault is still there). `human_action.json` shows the point of this run: `resume_decision: "none"` and `human_performed_pending_action: false`, mechanically derived from re-checking the real page, disagreeing with the operator's own good-faith note. |
| `replay-20260915235033/` | `cua set-scope --artifact .../2.json --base-url ... --allowed-route-pattern "/members/*"` (resets the real, already-approved v2 to `draft` -- see the row's `NOTE.md` for why this is a legitimate, not contrived, way to get there), then `cua replay --artifact .../2.json -p member_id=12345 --no-handoff` | `FAILED`, kind `not_approved`, in 0.37s -- refused before the browser is ever touched. `cua approve` was run immediately after to restore v2; `git diff` on the artifact is empty. |
| `recovery-refused-schema-20260915234907/` | attempted `Capability.model_validate(...)` on a hand-built artifact declaring `recovery.do="retry_step"` on a Click step | Real `pydantic.ValidationError` (`validation_error.txt`) -- the schema-level half of the retry_step guard (REPORT.md sec 3) refuses to let such an artifact be constructed at all, let alone saved. **Scripted, no browser** -- see the row's `NOTE.md`. |
| `recovery-refused-runtime-20260915234907/` | real `replay()` call against the same class of artifact, but with the offending `runtime_match`'s `after_step` set to `None` (the one shape the schema check can't catch statically) | `FAILED`, kind `recovery_refused`, `click_count == 1` -- the runtime backstop refuses the retry BEFORE a second click, not after one already landed twice. **Scripted surface** -- see `NOTE.md` for exactly what is and isn't real here. |
| `session-lost-20260915234907/` | real `replay()` + a real SQLite `ControlBroker` (two independent connections, one per thread, mirroring the two real OS processes `cua replay`/`cua ops` actually are) -- claim, then release with a note but no real fix, on a surface that reports an off-scope location after the "crash" | `FAILED`, kind `session_lost`, `observed: "now at http://evil.example.com/login"`. Also produced real `intervention.json` and `human_action.json` (`resume_decision: "none"`, matching the good-faith-note-vs-derived-fact point of `replay-20260915230202/`). **Scripted surface** -- see `NOTE.md`. |
| `discovery-handoff-cleared-20260917022631/` | real `run_discovery()` + a real `ControlBroker` across a real thread boundary -- a scripted, unresponsive page triggers a genuine no-progress stall, `cua ops claim`/`release --resolution cleared_obstacle` hands back a FRESH observation | discovery completes (`success: true`), `compile_capability()` emits a real `draft` artifact with `provenance.discovery_handoffs == 1`; a real `cua approve` on it (no `--validation-run`) is refused -- see `approve_refusal_without_validation.txt`. **Scripted model/surface** -- see `NOTE.md`. |
| `discovery-handoff-advanced-20260917022632/` | same stall, released with `--resolution workflow_advanced` (the operator did the task by hand instead) | discovery aborts (`success: false`, reason names `workflow_advanced`); `compile_capability()` refuses with the same, unmodified "cannot compile a failed discovery run" error (`compile_refusal.txt`) -- no new exception type needed. **Scripted model/surface** -- see `NOTE.md`. |

This is the seventh generation of this evidence set. Four rounds of review found
real, fixable gaps, each time in a versioned or committed file; a fourth round added
the two `cu_northgate` runs once the overlay resolver was actually built; a fifth
round closed a gap found by re-reading the assignment brief itself: three real
safety/control-flow mechanisms this project implements and unit-tests
(`not_approved`, `recovery_refused`, `session_lost`) had no evidence proving they fire
outside a test file. A sixth round added the two `discovery-handoff-*` runs once the
discovery-side handoff mechanism itself was built -- Section 3.6 of the brief lists
"the agent is stuck during discovery" as one of three cases needing human-in-the-loop,
and until this mechanism existed, only the other two (a stuck replay, an irreversible
step) were actually wired to it. A seventh round added the three `replay-20260916011636/`
`-011639/`/`-011641/` rows: `ACCOUNT_FROZEN` and `PERMISSION_DENIED` were derived,
approved into v3/v4, and verified live the same day the member fixtures for them were
built, but this index was never updated to include the runs that verified them -- found
by a README/REPORT.md/design-doc cross-check that counted the runs actually on disk
against the count this file claimed.

1. The compiler could anchor a locator on a dynamic table cell -- a results row's
   member name ended up recorded in the artifact -- and discovery's own CLI wrote
   the un-templated goal into its evidence even though `compile_capability` correctly
   templated it into the artifact. Fixed in `src/cua/agent/compile.py` (see
   `_nearest_label_to_the_left`) and `src/cua/cli.py`.
2. Discovery's captured *output value* itself -- the actual balance, name, or
   whatever the model read off the screen -- still reached evidence in the clear in
   three different places: the top-level `outputs` dict, the model's own `finish`
   tool call (which carries a second, nested copy of the same dict), and the "read"
   step's own observed `node.text` (the literal page text a read step's entire
   purpose is to capture). A regex-only redaction pass cannot catch this class of
   leak at all -- a plain name like "Dolores Ibarra" matches no SSN/email/digit-run
   pattern and passes straight through. Fixed by masking every one of those three
   unconditionally (`mask(value, "unclassified")`), the same way `ctx.*` values are
   already unconditionally masked in replay -- both are values captured live with no
   reviewed `OutputSpec.sensitivity` yet to gate on.
3. `Capability.status` (`draft` / `approved`) existed but nothing ever checked it --
   an unreviewed artifact could run unattended exactly like a reviewed one. Fixed by
   gating `replay()` on `status == "approved"` and adding `cua approve` to promote
   one. Hardening resets status back to `draft` on the artifact it produces, since a
   new `runtime_match` is new, unreviewed behaviour even when its base was approved.

Every discovery/replay/handoff file was regenerated after the three fixes above; grep this
whole tree for the member's name, id, or balance and you will not find any of them outside
of command-line examples in this file. The two `cu_northgate` runs are new, not
regenerated -- they didn't exist until `src/cua/overlay/resolver.py` did. The same is true
of the four newest rows (`replay-20260915235033`, both `recovery-refused-*`, and
`session-lost-20260915234907`): they didn't exist until this evidence gap was found by
re-reading the brief, and required no fix to anything -- the mechanisms they demonstrate
were already correctly implemented and unit-tested; only the live/real-code proof was missing.

## What each file is

- `transcript.json` (discovery only) -- every tool call the model made, the node it
  resolved against, and the result, in order, plus which model (`provider_model`)
  actually answered that step. This is the thing to read to convince yourself the
  discovery run is real and not scripted: it is the model's own sequence of
  decisions, not a fixed script. Structural/control text (the goal, non-output tool
  args, button/heading node text, location paths) gets discovery-time literals
  templated out plus a regex redaction pass. Anything that IS a captured output
  value -- the top-level `outputs` dict, `finish`'s own nested `outputs` arg, and a
  "read" step's `node.text` -- is masked unconditionally instead, since nothing has
  reviewed its sensitivity yet at this point in the pipeline.
- `run_meta.json` (discovery only) -- goal, target, the model(s) that actually
  reasoned (not just the configured primary -- see the fallback note above),
  success, step count, wall-clock duration. `outputs` here is masked the same way.
- `artifact_emitted.json` (discovery only) -- the exact capability artifact compiled
  from that transcript, before hardening added the `MEMBER_NOT_FOUND` runtime_match.
- `result.json` / `result_before_escalation.json` / `result_after_resume.json` --
  the replay engine's `ReplayResult`, matching the 4-way status contract described
  in `REPORT.md` sec 3 (`success` / `business_outcome` / `failed` / `escalated`).
  Outputs here are NOT masked the same way discovery's are: by replay time the
  artifact's `OutputSpec.sensitivity` has been reviewed (a hardening pass or a human
  sets it), so a declared, non-sensitive output like `savings_balance` is the
  capability's actual answer, not an incidental leak -- see `REPORT.md` sec 6.
- `s0-failure.png` -- captured by `WebAdapter.screenshot()` at the moment of failure,
  the richer failure signal `REPORT.md` sec 3.5 asks for alongside the structured log.
- `intervention.json` (handoff only) -- the payload routed to the operator: run id,
  capability, step, reason, expected vs. observed, screenshot ref, completed steps,
  session handle. Redacted the same way logs are (nothing sensitive appears here
  because this capability's only input is a non-sensitive member id).
- `human_action.json` (handoff only) -- what the operator actually did: who claimed
  it and when, before/after URL and screenshot, their own free-text note, and two
  fields that are deliberately not the same thing -- `resume_decision` (the literal
  value `find_resume_point` returned) and the derived `human_performed_pending_action`
  boolean. Both come from mechanically re-checking the live page against the stuck
  step's own checkpoint, never from the note or from anything self-reported --
  `replay-20260915230202/` exists specifically to show the derived fact disagreeing
  with a well-intentioned note.
- `validation_error.txt` / `attempted_artifact.json` (`recovery-refused-schema-*` only) --
  the literal `str(pydantic.ValidationError)` raised when trying to construct the
  artifact next to it, and the artifact itself, so a reader can see exactly which
  step/field triggered the refusal without re-running anything.
- `NOTE.md` (five rows only) -- what's real and what's scripted in that specific run,
  and why the mock target app can't produce that condition live. Present precisely
  because a claim of "real" needs a place to be honest about the one part that isn't.
- `compile_refusal.txt` (`discovery-handoff-advanced-*` only) -- the literal
  `str(ValueError)` `compile_capability()` raises for a discovery transcript whose
  handoff resolved `workflow_advanced`.
- `approve_refusal_without_validation.txt` (`discovery-handoff-cleared-*` only) -- a
  real, unmodified `cua approve` invocation on the artifact this run compiled, refusing
  to promote it without a `cua validate` run first. Needed no scripting: `cua approve`
  only ever inspects the artifact file, never touches a browser.

## How to check `--fault` isn't just short-circuiting the engine

`--fault "inject=500"` only appends `?inject=500` to the URL of the run's first
navigate step, for that one invocation -- it never touches the saved artifact and the
replay engine has no code that special-cases the string `"inject"` anywhere. The
query parameter is interpreted by the mock target app itself
(`src/cua/target_app/app.py`), which returns a genuine HTTP 500 page when it sees
`inject=500`. The engine then does exactly what it would do for any unexpected page:
observes the DOM, checks the step's checkpoint, fails to match it, and classifies the
divergence. `replay-20260915044645/result.json`'s `failure.observed` field
(`"at http://127.0.0.1:8800/members/search?inject=500"`) is the engine reporting
where it actually ended up, not a canned message -- and `s0-failure.png` is a
screenshot of that real error page, not a placeholder.
