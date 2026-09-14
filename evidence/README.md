# Evidence index

Every run below is real: a live Gemini call for discovery, a real headed Playwright
browser, a real local FastAPI target app, a real SQLite control-transfer broker for
the handoff run. Nothing here is hand-written or simulated. Reproduce any row with
the target app running (`cua serve-target`) and the command shown.

| Directory | Command | Result |
| --- | --- | --- |
| `discovery-20260914081535/` | `cua discover --goal "look up member 12345 and read their current savings balance" --target http://127.0.0.1:8800/members/search --name lookup_savings_balance` | Live Gemini-driven discovery, 5 steps, 21.7s. Emits `acme_core.lookup_savings_balance` v1. |
| `replay-20260914224748/` | `cua replay --artifact artifacts/acme_core.lookup_savings_balance/2.json -p member_id=12345` | `SUCCESS`. `outputs.savings_balance` is a typed `Money` object (`{"amount_minor": 816000, "currency": "USD"}`), not a raw string. |
| `replay-20260914224757/` | `cua replay --artifact artifacts/acme_core.lookup_savings_balance/2.json -p member_id=99999` | `BUSINESS_OUTCOME`, code `MEMBER_NOT_FOUND`, detected after step `s2`. Not a crash: the artifact declares this outcome and the engine matched it against the observed page. |
| `replay-20260914224802/` | `cua replay --artifact artifacts/acme_core.lookup_savings_balance/2.json -p member_id=12345 --fault "inject=500" --no-handoff` | `FAILED` at step `s0`, kind `checkpoint_failed`. `s0-failure.png` is a real screenshot of the target app's "System Error 500" page. |
| `demo-handoff-1789426299/` | same fault, with handoff enabled: `cua replay ... --fault "inject=500"`, then `cua ops claim <run_id>` / fix by hand / `cua ops release <run_id>` | `result_before_escalation.json`: `FAILED`, `intervention.json` raised (screenshot + reason + empty completed-step list, since it failed on step `s0`). Operator reloads the URL without `?inject=500` in the same browser window, releases control. `result_after_resume.json`: `SUCCESS` -- the resumed run re-checked its checkpoint and continued through to the same typed balance output. |

## What each file is

- `transcript.json` (discovery only) -- every tool call the model made, the node it
  resolved against, and the result, in order. This is the thing to read to convince
  yourself the discovery run is real and not scripted: it is the model's own
  sequence of decisions, not a fixed script.
- `run_meta.json` (discovery only) -- goal, target, model name, success, step count,
  wall-clock duration.
- `artifact_emitted.json` (discovery only) -- the exact capability artifact compiled
  from that transcript, before hardening added the `MEMBER_NOT_FOUND` runtime_match.
- `result.json` / `result_before_escalation.json` / `result_after_resume.json` --
  the replay engine's `ReplayResult`, matching the 4-way status contract described
  in `REPORT.md` sec 3 (`success` / `business_outcome` / `failed` / `escalated`).
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
divergence. `replay-20260914224802/result.json`'s `failure.observed` field
(`"at http://127.0.0.1:8800/members/search?inject=500"`) is the engine reporting
where it actually ended up, not a canned message -- and `s0-failure.png` is a
screenshot of that real error page, not a placeholder.
