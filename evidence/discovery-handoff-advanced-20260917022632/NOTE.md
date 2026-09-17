Real `run_discovery()`, a real SQLite `ControlBroker` across a real thread boundary,
real `claim()`/`release(resolution="workflow_advanced")` -- only the `LLMProvider` and
`SurfaceAdapter` are scripted. Same rationale as the sibling
`discovery-handoff-cleared-*/NOTE.md` for why: reliably forcing a real model call into a
no-progress stall on demand isn't practical, and the mechanism under test (the broker,
the phase/resolution enforcement, the abort path) is the real code regardless of what
surface it's observing.

This run demonstrates the OTHER resolution: the operator declares they did the actual
task by hand instead of just clearing an obstacle. `run_meta.json` shows
`success: false` and the reason naming `workflow_advanced` explicitly.
`compile_refusal.txt` is the real `ValueError` `compile_capability()` raises for this
transcript -- the existing, unmodified "cannot compile a failed discovery run" refusal
(no new exception type was needed for this case; `transcript.success = False` was
already sufficient to trigger it).
