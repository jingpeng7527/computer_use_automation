# Evidence index

Every run below is real: a live LLM call for discovery, a real headed Playwright
browser, a real local FastAPI target app, a real SQLite control-transfer broker for
the handoff run. Nothing here is hand-written or simulated. Reproduce any row with
the target app running (`cua serve-target`) and the command shown.

| Directory | Command | Result |
| --- | --- | --- |
| `discovery-20260915043330/` | `cua discover --goal "look up member 12345 and read their current savings balance" --target http://127.0.0.1:8800/members/search --name lookup_savings_balance` | Live discovery, 6 real steps, 6.5s. Emits `acme_core.lookup_savings_balance` v1. Gemini's free-tier daily quota was exhausted at the time, so `FallbackProvider` fell through to Groq mid-run -- `run_meta.json`'s `model` field (`"openai/gpt-oss-120b -> gemini-3.6-flash"`) and each step's `provider_model` say so honestly, rather than crediting the configured primary for a run it didn't fully do. |
| `replay-20260915043402/` | `cua replay --artifact artifacts/acme_core.lookup_savings_balance/1.json -p member_id=12345` | `SUCCESS`. `outputs.savings_balance` is a typed `Money` object (`{"amount_minor": 816000, "currency": "USD"}`), not a raw string. |
| `replay-20260915043436/` | `cua replay --artifact artifacts/acme_core.lookup_savings_balance/2.json -p member_id=99999` | `BUSINESS_OUTCOME`, code `MEMBER_NOT_FOUND`, detected after step `s2`. Not a crash: the artifact declares this outcome and the engine matched it against the observed page. |
| `replay-20260915043438/` | `cua replay --artifact artifacts/acme_core.lookup_savings_balance/2.json -p member_id=12345 --fault "inject=500" --no-handoff` | `FAILED` at step `s0`, kind `checkpoint_failed`. `s0-failure.png` is a real screenshot of the target app's "System Error 500" page. |
| `demo-handoff-1789446887/` | same fault, with handoff enabled: `cua replay ... --fault "inject=500"`, then `cua ops claim <run_id>` / fix by hand / `cua ops release <run_id>` | `result_before_escalation.json`: `FAILED`, `intervention.json` raised (screenshot + reason + empty completed-step list, since it failed on step `s0`). Operator reloads the URL without `?inject=500` in the same browser window, releases control. `result_after_resume.json`: `SUCCESS` -- the resumed run re-checked its checkpoint and continued through to the same typed balance output. |

This is the third generation of this evidence set. Two rounds of review found real
leaks, not just wording problems, each time in a versioned or committed file:

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

Every file below was regenerated after both fixes; grep this whole tree for the
member's name, id, or balance and you will not find any of them outside of
command-line examples in this file.

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

## How to check `--fault` isn't just short-circuiting the engine

`--fault "inject=500"` only appends `?inject=500` to the URL of the run's first
navigate step, for that one invocation -- it never touches the saved artifact and the
replay engine has no code that special-cases the string `"inject"` anywhere. The
query parameter is interpreted by the mock target app itself
(`src/cua/target_app/app.py`), which returns a genuine HTTP 500 page when it sees
`inject=500`. The engine then does exactly what it would do for any unexpected page:
observes the DOM, checks the step's checkpoint, fails to match it, and classifies the
divergence. `replay-20260915043438/result.json`'s `failure.observed` field
(`"at http://127.0.0.1:8800/members/search?inject=500"`) is the engine reporting
where it actually ended up, not a canned message -- and `s0-failure.png` is a
screenshot of that real error page, not a placeholder.
