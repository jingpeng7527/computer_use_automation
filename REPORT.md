# REPORT

A computer-use system that discovers a UI flow once with an LLM, compiles it into a typed
capability artifact, and then executes that artifact deterministically with no model in the
decision loop.

Target surface: a locally hosted mock core-banking portal (`src/cua/target_app/`), written
deliberately in a legacy style. Implementation language is Python; the browser is driven by
Playwright in headed mode.

This document is deliberately short, per the brief's own scope. The full reasoning, code walk-
throughs, and diagrams behind every decision below live in
[`docs/DESIGN_AND_IMPLEMENTATION.md`](docs/DESIGN_AND_IMPLEMENTATION.md); every claim here is
backed by a real, committed test or evidence run, indexed in full in
[`evidence/README.md`](evidence/README.md).

---

## 1. Architecture

Two execution paths share a perception layer and never call each other directly: discovery (an
LLM observes and decides, once) writes a typed capability artifact; replay (deterministic, many
times) reads it. Deleting `src/cua/agent/` entirely would not affect replay -- the production path
must not be able to invoke a model even by accident. A hardening pass sits between them: a
happy-path discovery run never encounters "no such member," so a second, no-model pass replays the
artifact with deliberately bad input and derives the resulting divergence -- observed, not guessed
-- into a declared business outcome.

Key decisions: perception is accessibility-tree-first, not DOM-first, since the a11y tree is the
one semantic description every surface (web, Windows UIA, macOS AX) exposes, and it's what makes
Section 4's multi-tenant story possible at all. The architecture is single-process and synchronous
-- no queue, no service split -- with every future service boundary already an interface
(`SurfaceAdapter`, `ArtifactStore`, `LLMProvider`, `ControlBroker`). The browser runs headed, not
headless, because human takeover of the *same* live session is a core requirement; a visible window
gives that for free, and the alternative (a remote debugging port) would be an arbitrary-code-
execution surface into a live banking session, not just another channel. Storage is split by access
pattern: artifacts/evidence are versioned JSON files (git already provides diffable review),
control-transfer state is SQLite (contended across processes, needs atomic conditional updates a
JSON file can't give for free). The LLM sits behind one interface (`LLMProvider.decide()`), called
only from discovery; Gemini is primary with Groq as a same-shape fallback used only if the primary
call itself errors, so a single provider hiccup can't fail the one run the brief says has to be
real.

## 2. Artifact schema

The artifact is a contract, not a transcript -- shaped so a calling agent, a human reviewer, and
the replay engine can all read the same file for different purposes. Six parts: identity and scope
(`capability_id`, `version`, `status: draft|approved`, `app_profile`); typed inputs (type, pattern,
`enum_values`, a load-bearing `sensitivity` tag the logger reads); typed outputs (`Money` is
`{amount_minor: int, currency: str}`, never a float, since this is regulated financial data);
declared outcomes (a top-level `possible_outcomes` list, cross-validated against `runtime_matches`
in both directions); ordered steps (action, layered target, checkpoint); and runtime conditions
plus a capability-level success condition.

Values are bound by reference in three namespaces -- `input.*` (caller-supplied), `ctx.*`
(extracted mid-run, kept in memory, treated as sensitive by default since nothing has classified it
yet), `output.*` (returned, declared only) -- never by literal, which buys parameterization and
PII-exclusion with the same mechanism. This isn't just asserted: a JSON scan of every committed
artifact (`tests/test_no_recording_time_literals_in_artifacts.py`) and a fully scripted
discover→compile→replay run with no API key or browser
(`tests/test_discovery_compile_replay_with_fake_provider.py`) both assert no recording-time literal
survives compilation.

Targeting is a ranked list, not a scored one: four strategies (role+name, relative-label anchor,
CSS/attribute, bounding box) tried in fixed order, first exact-match wins, and the winning layer is
recorded as the drift signal (Section 3). A scored/weighted alternative was rejected on three
grounds: the weights have no defensible origin, it breaks determinism (a small markup change could
silently shift which strategy wins between two runs), and it destroys the drift signal by blending
it into one number. Full schema and a worked example: `src/cua/schema/`,
`artifacts/acme_core.lookup_savings_balance/`.

## 3. Determinism and error handling

Replay executes a fixed step list with no planner and no model call; determinism comes from three
mechanisms working together, not from the absence of failure modes: layered target resolution
(above), explicit declared waits (never a fixed sleep), and checkpoints that assert a post-condition
rather than trusting that an action "probably worked."

Every observed non-happy-path state is classified into exactly one of three categories, and the
category shapes the response: **business outcome** (a legitimate answer like "member not found" --
stop cleanly, return the code); **recoverable** (a known interstitial or transient load -- run the
declared recovery, resume); **hard failure** (unsafe, ambiguous, or unexpected -- stop, capture
evidence, escalate). Terminal `runtime_matches` are checked *before* the step checkpoint, on a
single snapshot, because a page correctly reading "no member records match" would otherwise fail
the detail-page checkpoint and get reported as a crash -- this ordering is the actual mechanism by
which the brief's stated failure mode ("no such member" treated as an error) is avoided, not merely
described as avoided.

Recovery is bounded four ways (per-matcher `max_retries`, an artifact-level budget, a policy
ceiling the artifact can tighten but never raise, and a max recovery depth of one), plus a narrower
guard: any recovery action naming a `Click`/`TypeText`/`Select` step is refused, both at artifact-
validation time and as a replay-time backstop, because a failed checkpoint never proves a write
didn't already take effect -- a slow POST looks identical to one that never fired. This guard is
keyed on the action's own type, never on the artifact's self-reported `risk_level`, for the same
reason risk tiering is (Section 6): data doesn't get to vouch for its own safety. Real evidence for
both layers, not just unit tests: `evidence/recovery-refused-schema-*/` (a real
`pydantic.ValidationError`) and `evidence/recovery-refused-runtime-*/` (a real `replay()` run
proving the guard fires before a second click, not after).

Drift is a secondary, cross-run signal, not a runtime recovery path: `cua drift-report` aggregates
which locator layer resolved each step across every past run, and flags a *persistent* demotion
(several runs in a row landing deeper than baseline) as `drifting`, distinct from a one-off
`occasional` blip -- collapsing that distinction would train reviewers to ignore the signal.

## 4. Heterogeneity and multi-tenant

The surface seam (`SurfaceAdapter`: observe/resolve/act/wait_for/location) is designed for a
desktop or vision adapter, not built as one -- the brief doesn't ask for a second implementation,
only for abstractions that don't foreclose it, and nothing above the seam names a CSS selector, a
frame, or a DOM node. The mock portal is deliberately written to defeat each targeting layer in
turn (a real `<label>`, an unlabelled input anchored by adjacent text, a `<div onclick>` control, an
unreachable image-only tab), so the layer ranking is tested against real markup, not asserted.

Multi-tenant reuse **is** built and demonstrated, not just argued. Institutions running the same
vendor product diverge in more than labels -- a required extra field, a different entry route, a
skipped confirmation step -- so overlays support four operations (`replace_target`,
`replace_value`, `insert_after`, `skip`), not just relabeling, and a resolved overlay is
re-validated by round-tripping through the same `Capability.model_validate()` every base artifact
goes through, catching a broken reference before a browser is ever involved. The mock app ships a
genuine second tenant (`cu_northgate`, different labels, an actually-`required` branch selector the
base tenant lacks, a differently-classed control) and the *same resolved artifact* returns both
`SUCCESS` and `BUSINESS_OUTCOME/MEMBER_NOT_FOUND` against it --
`evidence/replay-20260915224332/` and `-224345/`. An overlay also declares its own scope
(`allowed_route_patterns`), narrowing -- never widening -- the global allowlist; pointing the same
resolved artifact at the base tenant's routes instead is refused, verified live, not just claimed.
`cua drift-report` groups by `capability_id`, so a tenant's overlay resolution is tracked
independently from its base for exactly this degradation signal.

## 5. Escalation and handoff

Four explicit triggers, not timeout-based: all target layers exhausted, a checkpoint fails with no
matching `runtime_matches` entry, an `IRREVERSIBLE` step needs authorization, or (discovery only) N
consecutive steps produce no state change. An intervention carries capability/goal, step, reason,
expected-vs-observed, a screenshot, and completed steps -- enough to act without reconstructing the
run.

**Replay-side control transfer** is a SQLite broker, not a service: the operator reaches the live
session by clicking the window already in front of them, so the broker's only job is one row saying
who's in control, with conditional atomic claims (`UPDATE ... WHERE state='PAUSED'`, checked by row
count) so two simultaneous claimants can't both win. Control is a lease, not permanent ownership,
expiry is evaluated lazily at the next read/claim rather than by a background job (this is a
single-process, synchronous system -- nothing should have to run continuously for a lease to free
up), and the executor re-checks its holder status before *every* action, not just once while
waiting. Resuming checks exactly two candidates -- the capability's own success condition, then the
stuck step's checkpoint -- never a general "where am I" scan, since checkpoints collide by design
across a flow's different stages. If neither holds and the session has also drifted outside the
allowlisted scope, that's reported as `session_lost`, a declared failure kind, rather than retried
as an ordinary give-up. Every handoff writes `human_action.json`: before/after URL and screenshot,
the operator's own note, and a `human_performed_pending_action` fact derived *mechanically* by
re-checking the page -- never taken on the operator's word, which `evidence/replay-20260915230202/`
demonstrates directly (a good-faith note and the derived fact disagree, on purpose).
`evidence/session-lost-20260915234907/` exercises the session-loss path against real broker/engine
code, since the mock app has no session-expiry mechanism to trigger it live.

**Discovery-side handoff is a different mechanism**, not replay's reused: `StepLog` only records
LLM tool calls, so if a human's own clicks cleared a stall, reusing replay's resume logic would
silently produce an artifact missing exactly the steps that got it unstuck. Instead the operator
declares a structured `resolution` -- `cleared_obstacle` (hand back to the LLM with a fresh
observation) or `workflow_advanced` (the operator did the task by hand; the run aborts, no
artifact) -- enforced by the broker itself, not the CLI. A handoff-touched artifact
(`discovery_handoffs > 0`) cannot be approved directly: `cua validate` must first prove it replays
clean, unattended, from a fresh session before `cua approve --validation-run <id>` will promote it.
`evidence/discovery-handoff-cleared-*/` and `-advanced-*/` run both resolutions against real
discovery/broker code (scripted model and surface, since reliably forcing a live LLM call to stall
on demand isn't practical).

## 6. Safety

Four layers, all enforced in the executor, never trusted to the artifact -- data doesn't get to
authorize its own actions. **Allowlist**: configured domains/routes/action-types, default-deny. A
capability can additionally declare its own scope (`AppProfile.base_url`/`allowed_route_patterns`),
and the executor takes the intersection with the global policy -- narrowing only, checked against
the global list first, so an artifact can never grant itself a destination policy doesn't already
permit. `cua set-scope` also pre-flights the *opposite* mistake: a new scope that would already
exclude one of the capability's own literal navigate targets is refused up front (fail-loud), not
just fail-safe, unless `--force` overrides it -- verified live against a real artifact. **Risk
tiers** (`SAFE_READ`/`REVERSIBLE_WRITE`/`IRREVERSIBLE`) are derived by the executor from the action
and resolved target, never accepted from the artifact; `IRREVERSIBLE` is refused by default because
the cost is asymmetric -- a blocked legitimate action costs a minute, an unauthorized irreversible
one in a banking system doesn't undo by retrying. **Approval gate**: `replay()` refuses anything not
`status=approved` before touching a browser; any operation that changes enforced behavior (hardening,
scope, output-sensitivity tagging, overlay resolution) resets an artifact back to `draft`, so
approval can't be inherited past a change it never reviewed -- demonstrated live in
`evidence/replay-20260915235033/` (a real approved artifact, reset and refused, restored after).
**Sensitive data**: structural exclusion (values are bound by reference, so there's nothing to
redact for a step's own literal) plus runtime redaction (a `sensitivity`-tag-driven mask, and
unconditional masking for anything captured live with no reviewed sensitivity yet -- a plain name
matches no regex pattern, which was verified as a real leak before this existed). Execution is also
bounded on step count, wall-clock, per-wait timeout, and recovery budget, so a bad artifact fails
closed rather than exploring indefinitely.

## 7. Cuts

**Cut, with the interface present:** desktop/vision adapters (the seam is designed for them;
`SurfaceAdapter` has nothing browser-specific above it); an operator web console (explicitly
mockable per the brief; the CLI drives a real broker); queues/workers/service split (explicitly not
rewarded; every future cut point is already an interface); Postgres/object storage (files satisfy
reviewability at this scale).

**Cut outright:** general conditional branching in the artifact (a step list stays a reviewable
list, not a small language; "pick one of several matches" is a different, composable capability,
not a branch); automatic re-authentication after session loss (needs stored credentials and
produces a new session, which the takeover requirement rules out -- reported honestly as an outcome
instead); LLM-assisted recovery on replay failure (reintroduces a model into the production path;
would need its own budget and policy gate to be safe, so left out rather than done loosely);
multi-run flakiness scoring (the feeding mechanism, layer-hit telemetry, exists; the scoring itself
does not).

**What I'd build next, in order:** wire `cua drift-report`'s exit code into CI; a desktop adapter
against one real legacy application, since an argument isn't a test; bounded assisted recovery with
the budget/policy checks it requires; a signed review behind the approval gate, rather than a CLI
command anyone can run.

**The one thing that is not a cut:** the discovery run is real. Seventeen runs are committed and
indexed in full in `evidence/README.md`, spanning a live LLM-driven discovery
(`discovery-20260915043330/`), success/business-outcome/hard-failure replay, a real live handoff
with an operator fixing an injected fault in the same browser session
(`demo-handoff-1789447614/`), the multi-tenant overlay evidence above, live replays proving all
three declared outcomes (`MEMBER_NOT_FOUND`/`ACCOUNT_FROZEN`/`PERMISSION_DENIED`) against their
own member fixtures, and six further runs (`not_approved`/`recovery_refused` × 2/`session_lost`/
discovery-handoff × 2) added specifically because each mechanism was already real, implemented,
and unit-tested, but had no evidence proving it fires outside a test file -- a gap found by
re-reading the brief's own evidence requirement, not a bug in the mechanisms. Every path named
above and throughout `docs/DESIGN_AND_IMPLEMENTATION.md` resolves to a real file in that tree.
