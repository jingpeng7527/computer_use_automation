# REPORT

A computer-use system that discovers a UI flow once with an LLM, compiles it into a typed
capability artifact, and then executes that artifact deterministically with no model in the
decision loop.

Target surface: a locally hosted mock core-banking portal (`src/cua/target_app/`), written
deliberately in a legacy style. Implementation language is Python; the browser is driven by
Playwright in headed mode.

---

## 1. Architecture

Two execution paths that share a perception layer and never call each other directly.

```
DISCOVERY (once, model in the loop)        REPLAY (many times, no model)

goal + target                             capability_id + typed params
      |                                          |
      v                                          v
  Agent Loop  <--- observe/decide --->  LLM   Replay Engine
      |                                          |
      v                                          |
  Hardening pass (bad input, no model)           |
      |                                          |
      +----------------+-------------------------+
                       |
                 Surface Adapter          <-- the seam
                       |
                 Live Session (one browser)
                       ^
                       |
                  Human Operator          <-- same session, not a new one

cross-cutting: Policy Gate | Observability | Control Broker
```

The only thing the two paths share is the artifact. Discovery writes it; replay reads it.
Deleting `src/cua/agent/` entirely would not affect replay. That independence is the point:
the production path must not be able to invoke a model even by accident.

**Key decisions and trade-offs**

*Perception is accessibility-tree-first, not DOM-first.* The DOM is richer and more precise,
but it only exists in a browser. The a11y tree is the one semantic description that browsers,
Windows UI Automation and macOS AX all expose, and it describes what an operator sees rather
than how the markup was written. Legacy enterprise markup is nested tables with generated ids;
the a11y projection of that same screen is still `textbox "Member ID"`. Choosing a11y as the
primary targeting vocabulary is what makes Section 4 possible at all. The cost is that a11y
metadata on legacy screens is often incomplete, which is why targeting degrades through layers
rather than betting on one.

*Single process, synchronous, file-backed.* One Python process runs the loop, the browser and
the mock app. No queue, no worker pool, no service split. The brief explicitly does not reward
scaling infrastructure, and every boundary that would matter later is an interface today
(`SurfaceAdapter`, `ArtifactStore`, `LLMProvider`, `ControlBroker`). Those are the four seams
where this would be cut into services; none of them are cut now.

*Headed browser, not headless.* Headless is the normal default and is faster. Headed is chosen
because human takeover of the *same* live session is a core requirement, and a visible window
gives that for free: automation pauses, the operator clicks the window that is already open.

Two things are worth separating here, because they are often conflated. The *mechanism* is that
automation stops touching a session it keeps alive, and a human drives that same session; it is
independent of how the session is displayed and holds equally in a container. The *channel* is
how the operator sees and reaches that window. In this implementation the channel is a local
headed window, which is a deliberate simplification. The production channel is the same browser
in the same container under Xvfb, surfaced over noVNC to a central console; that changes the
transport and nothing above it. What is not proposed is exposing a remote debugging port for
this purpose. A debugging protocol grants arbitrary script execution in the page and arbitrary
navigation, so handing it to an operator console is an arbitrary-code-execution surface into a
live authenticated banking session, not merely one more channel. It is also unnecessary: the
operator needs pixels and input, which a pixel stream provides without any of that authority.

*Storage is split by access pattern, not unified for tidiness.* Artifacts and evidence go to
disk as JSON/JSONL, because the brief requires artifacts to be versioned and reviewable, and
git already provides both: a diff on an artifact is a reviewable change to a capability.
Control-transfer state goes to SQLite, because it is contended across processes and needs
conditional atomic updates. Implementing that correctly on a JSON file means hand-rolling
cross-platform file locking, which is more risk than the dependency is worth. SQLite is a
single file in the standard library, so this costs no operational surface.

*A successful run is not a production artifact, so discovery has two phases.* The happy-path run
never encounters "no member records match", which means an artifact emitted straight from it
declares no business outcomes at all and the error taxonomy in Section 3 would be empty in
practice. A hardening pass closes that gap: the same flow is re-run with a deliberately invalid
input, the step at which it diverges is recorded, and the observed state at that point becomes a
`runtime_matches` entry and a `possible_outcomes` code. It costs a handful of steps, it needs no
model, and the resulting condition is observed rather than guessed, which matters because a
model asked to speculate about failure modes will produce plausible strings that never appear on
screen. It also produces the not-found evidence run for free. Known interstitials and other
recoverable conditions are seeded the same way where the mock app can trigger them, and beyond
that they accumulate through operator escalations, as described in Section 4.

*Classification comes from divergence behaviour, not from semantic understanding.* The obvious
question about the hardening pass is how anything decides that the diverged state is a business
outcome rather than a crash. The answer is that no model makes that call, and no list of expected
outcomes is supplied in a prompt; either would mean the taxonomy was authored rather than
discovered. What the pass has is two runs of the same step list under different inputs, and the
classification is read off mechanical properties of the divergence: whether the surface reached a
stable rendered state or an error surface, whether the flow can proceed or is terminal, and
whether the same input reproduces the same divergence. A stable terminal page carrying no error
signal is recorded as a business outcome; an error surface or an unrenderable state is recorded
as a hard failure; a state that clears on its own or after a declared dismissal is recorded as
recoverable. The code name (`MEMBER_NOT_FOUND`) is a label applied at curation time and is the
one part a human confirms before the artifact is promoted from `draft`. Naming is cheap to
review; classification is not, which is why classification is derived and naming is not.

*LLM provider is behind an interface and used only in discovery.* The model is asked for one
structured decision per step, via tool calling, and never for free-form text. Swapping providers
is a new class implementing `LLMProvider.decide()`.

The implementation calls Gemini as the primary provider, with an open-weight model served by
Groq (through its OpenAI-compatible endpoint) as a secondary fallback used only if the primary
call itself raises — `FallbackProvider` tries Gemini first and only reaches for Groq on an
error, not as a load-balanced choice. Two-provider redundancy was chosen over a single provider
because a discovery run's one non-negotiable requirement is that it actually completes; a
transient error or rate limit on one provider shouldn't be able to fail the whole run when a
second, independently-hosted model can pick up the exact same tool-calling contract. The
perception choice makes provider vision capability irrelevant either way: the model is shown a
normalised list of interactive nodes, not a screenshot, so what's required of it is tool calling
over a text observation rather than coordinate estimation, and a purpose-built computer-use
model would be answering a question this design doesn't ask.

The hosted model catalogue rotates faster than expected: `gemini-2.5-flash` was retired
mid-build ("no longer available to new users"), which is exactly the scenario the interface is
for. The fix was a one-line default change (`CUA_LLM_MODEL=gemini-3.6-flash`), not a code
change — the contract in this system is `LLMProvider.decide()`, and the model id is
configuration, read from the environment, not architecture.

---

## 2. Artifact schema

The artifact is a contract, not a transcript. It is shaped so that three different readers can
use the same file: a calling agent needs the signature, the replay engine needs the steps, and
a human reviewer needs to be able to approve it in a pull request.

Six parts:

**Identity and scope.** `schema_version`, `capability_id`, `version`, `status`
(`draft | approved`), and an `app_profile` naming the vendor product and version this was
recorded against. `status` is not decorative: replay refuses anything not `approved` before it
touches a browser (Section 6), and `cua approve` is the one-line promotion a review gates. Recording
provenance is kept because a capability is only meaningful relative to the surface it was learned
on. Three version numbers appear in the file and they are not interchangeable: `schema_version`
versions this format, `version` versions the capability itself
(the flow changed), and `app_profile.version` records the vendor product build the flow was
learned against. Only the last one is a fact about the world; the first two are ours.

**Typed inputs.** Name, type, validation pattern, required flag, and a `sensitivity` tag. The
sensitivity tag is load-bearing rather than documentation: the logger reads it and redacts on
that basis, so redaction does not depend on anyone remembering.

**Typed outputs.** Declared shape of what the caller receives. Money is represented as
`{ amount_minor: int, currency: str }`, never a float. This is regulated financial data and
binary floating point cannot represent decimal currency exactly.

**Declared outcomes.** A top-level `possible_outcomes` list of the business outcome codes this
capability can return, alongside `inputs` and `outputs` rather than buried in the runtime rules
below, and validated at construction time to match `runtime_matches` in both directions: a code
with no rule that can produce it is rejected exactly like a rule producing an undeclared code.
The implemented capability's list, after the hardening pass, is `["MEMBER_NOT_FOUND"]` — the one
divergence actually observed against the mock portal. `ACCOUNT_FROZEN` and `PERMISSION_DENIED` are
real business-outcome codes this design accounts for (Section 4) but were not exercised: producing
them needs member fixtures the target app doesn't have, cut for time rather than silently dropped
— see Section 7. Session loss during a handoff is a different case, and is implemented: it is a
`FailureKind` (`session_lost`), not a business outcome, since a session disappearing mid-run is a
failure the caller needs to know about, not a legitimate answer. `tests/test_escalation_wiring.py`
exercises it directly. What's still not exercised is the trigger condition against the real mock
app: it has no session-expiry mechanism to fire it live, only a scripted adapter that simulates one
landing outside the allowlisted scope. Failure codes are not declared here because they are system-level and identical
across capabilities; business outcomes are specific to what this capability can legitimately
answer. A calling agent needs to know that `lookup_savings_balance` can answer `MEMBER_NOT_FOUND`
before it invokes it, and it should not have to read detection rules to find that out, any more
than an HTTP client reads server code to learn that 404 is possible. The contract declares what
can come back; `runtime_matches` is the engine's business.

**Ordered steps.** Each step carries an `action`, a `risk_level`, a layered `target`, an optional
`wait`, and a `checkpoint`. Steps bind values by reference and never by literal, which serves
parameterisation and PII-exclusion with the same mechanism: the concrete member id used during
recording is structurally absent from the file.

References live in three namespaces, and the distinction is a data-handling rule as much as a
naming one:

| Namespace | Source | What the file holds |
|---|---|---|
| `input.*` | supplied by the caller per invocation | the reference only |
| `ctx.*` | read off the surface during this run | the reference only; values stay in memory |
| `output.*` | returned to the caller | the declaration only |

`ctx` exists because a flow that can only consume caller-supplied values is limited to lookups. A
step that extracts a server-generated token (`into: ctx.txn_token`) and a later step that types it
back (`value_from: {{ctx.txn_token}}`) is the minimum needed for anything transactional, and
without it the schema quietly restricts the system to read-only capabilities. Because every `ctx`
value comes off a live screen, it is treated as sensitive by default rather than by tag: `ctx`
values are never written to logs in the clear, and a `ctx` reference is the only kind whose
sensitivity is not declared in the artifact.

**Runtime conditions and success.** A top-level `runtime_matches` list of recognised non-happy-path
states, each classified as business outcome, recoverable or hard failure, plus a top-level
`success_condition` for the capability as a whole. Per-step checkpoints prove each step landed;
the success condition proves the capability delivered. Recoverable entries also carry their own
bounds (`max_retries`), which Section 3 completes with the capability's own aggregate budget and,
underneath that, a policy-level ceiling the artifact can tighten but never raise.

**Targeting is a ranked list, not a selector.** Four strategies in fixed order:

| Layer | Strategy | Why it is at this rank |
|---|---|---|
| 1 | `role` + accessible name | Portable across web and desktop; survives layout change |
| 2 | Relative label anchor | Works when a control has no accessible name, which is common on legacy screens: find the text "Member Number:" and take the adjacent input |
| 3 | Robust CSS / attribute | Web-only fallback; generated ids and hashed classes are excluded |
| 4 | Visual bounding box | Last resort for canvas or image-rendered surfaces |

Layers 3 and 4 are declared by the web adapter and ignored by any other adapter. The ranking
carries the design argument directly: everything portable is above everything browser-specific.

**Resolution is ordered first-wins, not scored.** An alternative considered and rejected was to
evaluate every strategy, weight the results and take the highest-scoring candidate above a
threshold. It is rejected on three grounds. The weights and the threshold have no defensible
origin, and this document would then contain numbers that cannot be explained. It breaks
determinism, which is the whole point of the replay path: under a weighted scheme a small markup
change can silently shift which strategy wins between two runs of the same artifact on the same
input. And it destroys the drift signal, because a blended score cannot tell a reviewer that
layer 1 stopped working. Strategies are therefore tried in rank order, the first one that
resolves to exactly one element wins, and the winning layer is recorded. Ambiguity is treated as
non-resolution: a strategy matching several elements is skipped rather than disambiguated by
position.

That last point is why positional targeting is excluded from the ranked layers. "The third input
on the page" survives nothing: a tenant adding one field shifts every index. Ordinals are
available only inside a relative-label anchor, scoped to one container, never as a page-level
strategy.

**No static confidence scores.** An earlier draft attached a fixed `confidence` to each strategy.
That number had no defensible origin. Strategy reliability is instead measured: replay records
which layer resolved each target, and sustained fallback to a lower layer is the drift signal
described in Section 4. Reliability is observed, not asserted. Each strategy does carry one
free-text `rationale` field — never read by the resolution algorithm, purely for a human
reviewer — which is where the "reasoning about robustness" the brief asks for actually lives;
this is documentation, not a second confidence score, and nothing in the engine consumes it.

The full schema, as Pydantic models plus a worked example built against a real discovery run, is
in `src/cua/schema/` and `artifacts/acme_core.lookup_savings_balance/`. Pydantic gives runtime
validation, JSON round-tripping and JSON Schema export in one definition, so the schema is
simultaneously the contract published to callers and the validator that rejects a malformed
artifact before it can touch a browser.

---

## 3. Determinism and error handling

**Determinism.** Replay reads the step list and executes it in order. There is no planner, no
retry-with-variation, and no network call to any model. Same artifact plus same inputs yields
the same action sequence every time, which is what makes the path auditable: the file is the
plan, and the file can be printed and reviewed before it ever runs.

Three mechanisms keep that from being brittle:

1. *Layered target resolution.* Strategies are tried in rank order. First resolution wins, and
   the layer that won is logged.
2. *Explicit waits, never sleeps.* Each step declares what it waits for (element visible,
   network idle) with a timeout. Fixed sleeps are absent because they are the usual source of
   both flakiness and wasted latency.
3. *Checkpoints instead of optimism.* A step is not complete because the click returned. It is
   complete when the declared post-condition holds. This localises failure to the step that
   actually broke, which is what makes the failure report debuggable.

`wait` and `checkpoint` are separate fields doing separate jobs, and the executor treats them
that way. `wait` settles the surface after an action; exhausting it is a timing signal that
routes to the recoverable branch. `checkpoint` then asserts, against a freshly taken snapshot,
that the state named in the artifact actually holds. Both are read from the artifact and neither
is hardcoded in the executor: the executor knows how to assert, not what to assert. Concretely,
a step is committed only after `act` returns, `wait` is satisfied, and a second `observe` plus
assertion agrees. A URL change, an HTTP 200 or a click that did not throw are never accepted as
evidence that a step landed.

**Error handling.** The brief is explicit that the interesting failures are runtime states, not
layout drift, and that conflating a business outcome with a crash is the common design mistake.
Every observed non-happy-path state is classified into exactly one of three categories, and the
category determines the shape of the response.

| Category | Example | Behaviour | Returned as |
|---|---|---|---|
| Expected business outcome | "no member records match", account frozen | Stop cleanly, return the finding | `BUSINESS_OUTCOME` with a code |
| Recoverable condition | session-warning interstitial, slow load | Run the declared recovery, resume the main flow | transparent to the caller, logged |
| Hard failure | HTTP 500, all target layers exhausted, unknown blocking dialog | Stop, capture evidence, escalate | `FAILURE` with step, expected, observed |

The order of evaluation is load-bearing, and it runs against a single snapshot. After `wait`
settles, the executor observes once and uses that one observation for everything that follows:

```
act -> wait -> observe (once)
  |
  +-- match terminal runtime_matches first
  |      business outcome -> stop cleanly, return the code
  |      recoverable      -> run declared recovery, retry this step (bounded)
  |
  +-- then assert the step checkpoint
         holds        -> step committed, continue
         does not hold -> match remaining matchers on the same snapshot
                            match    -> classify as above
                            no match -> FAILURE with evidence, then escalate
```

Terminal signals are matched before the checkpoint rather than after it, because a page reading
"no member records match" will never satisfy a checkpoint expecting the member detail heading.
Waiting for that assertion to time out would turn a business outcome into a slow one and,
depending on the branch taken, into a reported failure. Matching first costs one pass over a
snapshot that has already been taken.

The reverse ordering is the concrete mechanism by which "no such member" becomes a crash: assert,
raise on the failed assertion, never consult the signal table. A failed checkpoint is not itself
a failure; it is the trigger for classification.

The first row is the one that matters. A lookup for a member id that does not exist is a correct
answer to the caller's question, not an exception. Raising it as a failure would flood operations
with non-incidents and bury the real ones. Concretely, three result shapes:

```json
{ "status": "success",
  "outputs": { "savings_balance": { "amount_minor": 816000, "currency": "USD" } } }

{ "status": "business_outcome",
  "outcome": { "code": "MEMBER_NOT_FOUND", "description": "s2_business_outcome",
    "detected_after_step": "s2", "outputs": {} } }

{ "status": "failed",
  "failure": { "step_id": "s0", "kind": "checkpoint_failed",
    "expected": "reached a page headed 'Member Servicing Console'",
    "observed": "at http://127.0.0.1:8800/members/search?inject=500",
    "screenshot_ref": "evidence/replay-20260915044645/s0-failure.png" } }
```

(field names and casing above are the actual `ReplayResult`/`FailureDetail`/`BusinessOutcomeResult`
shapes, taken from committed evidence rather than restated from memory.)

**Recovery is bounded in four ways, and the structural one does most of the work.** Recovery
actions are declared on the `runtime_matches` entry that detects the condition, not on a step, so
a given interstitial is described once and handled wherever it appears. Four bounds apply:
`max_retries` per matcher, the capability's own aggregate recovery budget,
`policy.execution_bounds.recovery_budget_per_run` as the actual hard ceiling the artifact's own
budget can tighten but never exceed, and a maximum recovery depth of one. The policy ceiling
matters for the same reason risk tiering is assigned by the executor rather than trusted from the
artifact (Section 6): a capability's own declared budget is data, and data does not get to widen
its own limits.

The depth bound is the important one. A recovery action does not itself get a recovery branch: it
runs, control returns to the main flow, and the step is retried. If the condition reappears it
counts against that matcher's retries, and exhausting them is a hard failure. Oscillation between
a main flow and a recovery flow is therefore impossible by construction rather than caught by a
counter, which matters because the counter is exactly what a nested recovery would evade.

This class also absorbs what looks like a need for conditional branching. "A compliance notice
appears on the first login of the month and not otherwise" needs no branch: it is a recoverable
matcher, so it is dismissed when present and never matches when absent. Optional interstitials
are a recovery concern, not a control-flow concern, and this is the main reason the category
exists.

**Drift, secondarily.** Because the underlying UIs change slowly, drift is treated as a signal to
collect rather than a condition to recover from at runtime. Each resolution records the layer
that succeeded. Repeated demotion from layer 1 to layer 2 across runs marks the artifact for
review. The system does not attempt to self-heal a changed UI during a replay; silently adapting
is exactly the non-determinism the artifact exists to remove.

---

## 4. Heterogeneity and multi-tenant

Designed, not built. The brief asks for abstractions that do not paint us into a corner, and
explicitly does not ask for desktop or multi-tenant implementations.

**Surface abstraction.** The seam is `SurfaceAdapter`, and it is narrow on purpose:

```python
class SurfaceAdapter(Protocol):
    def observe(self) -> SurfaceSnapshot: ...   # normalised node tree + optional screenshot
    def resolve(self, target: Target) -> Handle: ...
    def act(self, handle: Handle, action: Action) -> None: ...
    def wait_for(self, predicate: Predicate, timeout_ms: int) -> bool: ...
    def location(self) -> Location: ...         # web: route; desktop: window + view id
```

`wait_for` is on the seam rather than hidden inside `resolve` because Section 3 forbids fixed
sleeps, which means waiting has to be a declared, bounded, first-class operation that an artifact
can specify and a log can record. `location` is deliberately not called `current_url`: the
allowlist check and the drift fingerprint both need to know where the session is, but a URL is a
browser fact and a desktop adapter would have to fake one.

That naming choice generalises into the rule for this interface: anything a desktop or canvas
adapter could not honestly implement stays out. Frame or document switching stays out, because
only web surfaces have frames and `WebAdapter` can resolve within the correct frame internally.
Dialog handling gets no dedicated method, because a dialog is a node with `role=dialog` and the
three core operations already reach it. Both were considered; adding either would put a browser
concept above the seam, which is the one thing that would make Section 4's argument false.

Above the seam, steps are expressed only in vocabulary that every surface has: role, accessible
name, relative label, text. Below the seam, an adapter decides how to satisfy that. `WebAdapter`
(Playwright) is implemented. `DesktopAdapter` (Windows UIA / macOS AX) and `VisionAdapter` exist
as the interface only, and the reason they are credible rather than aspirational is that nothing
above the seam names a CSS selector, a frame, or a DOM node. The browser-specific strategies live
inside the web adapter's declared capabilities, which other adapters simply do not advertise.

A legacy web app is the same adapter with worse input, which is precisely the case the layer-2
relative-label strategy exists for. It is exercised by the mock app rather than argued
hypothetically. The mock portal is written to defeat each layer in turn, so the ranking is tested
rather than asserted:

| Markup on the mock portal | Layer that has to carry it |
|---|---|
| `<label for>` bound to its input, real `<button>` text | 1, accessible name present |
| Nested table cell reading `Member Number:` beside an unlabelled `<input id="ext-gen-1029">` | 2, no accessible name, anchor off the adjacent text |
| `<div onclick>` acting as a control, no role and no `aria-label` | 3, web-only attribute match |
| Navigation tab rendered only as `<img src="tab_sav.gif">` with no alt text | none; resolution is exhausted |

The last row is deliberate. A target that no layer can reach is what produces the hard-failure
and escalation evidence in Section 5, and a mock app where every degradation is caught would
prove only that the happy path works.

The portal is fixtured so that one capability exercises every branch of the result contract
rather than only the successful one. Four member records cover the outcomes (present, absent,
frozen, above the operator's permission tier); query switches inject a session-warning
interstitial, a slow load, a compliance notice and a server error; and one control on the detail
screen is an irreversible action, so the risk tier in Section 6 can be shown refusing rather than
described. Being able to trigger these on demand is the whole reason the target is local: a
public demo site cannot be made to time out, cannot be given a second tenant variant, and comes
with terms and rate limits attached. Controllability is the argument for building it, not
convenience. Note that a control carrying an explicit `role` and
`aria-label` is accessibility done well, not legacy markup; it exercises layer 1, not the
fallbacks.

**Multi-tenant reuse.** Many institutions run the same vendor product with different branding,
labels and versions. Re-recording per tenant would mean thousands of artifacts and no way to
propagate a vendor upgrade, so artifacts are layered:

```yaml
# base, maintained once per vendor product
capability_id: "acme_core.lookup_savings_balance"
version: 3

# tenant overlay, stores differences only
extends: "acme_core.lookup_savings_balance@3"
tenant_id: "cu_first_national"
overrides:
  - op: replace_target
    step_id: "fill_member_id"
    target:
      strategies:
        - { type: role_name, role: textbox, name: "Acct / Member #" }
  - op: insert_after            # this tenant has a mandatory branch selector
    step_id: "fill_member_id"
    step: { id: "select_branch", action: select, ... }
  - op: skip                    # this tenant has no confirmation page
    step_id: "confirm_page"
```

Three operations rather than one, because real tenant divergence is not only relabelling.
Institutions running the same product add a required field or omit a confirmation screen, and an
overlay that can only rewrite a target cannot express either, which would force a re-recording for
exactly the cases layering exists to avoid.

Topology edits do create the failure the inheritance objection points at: skip a step whose
extracted value a later step consumes and the flow breaks in a way that is invisible in the
overlay. The answer is validation rather than a different composition model. Overlay resolution
runs a reference-integrity check: every `input.*`, `ctx.*` and `output.*` reference in the
resolved step list must have a producer that survives, and every `step_id` mentioned must exist in
the base. An overlay that breaks either is rejected at load time, before a browser is involved.
Slots were considered and rejected: a slot requires the base to anticipate where tenants might
extend it, which makes the base aware of its tenants and breaks the one-way dependency below.

Two properties are deliberate. The dependency points one way: an overlay names its base, and a
base never enumerates its tenants, so onboarding an institution does not edit a shared file.
And overlays store deltas, so most tenants have an empty overlay and a vendor-level fix reaches
all of them at once.

**Drift detection and graceful degradation.** Per-tenant replays report which target layer
resolved each step. A tenant that starts consistently falling to layer 2, or failing outright,
is flagged for review rather than discovered through a production incident. When a base artifact
does not fit a tenant, the run does not crash: it escalates per Section 5, the operator's manual
resolution is captured, and that capture is the raw material for the tenant overlay. Degradation
produces the fix instead of merely reporting the problem.

Cut from this section: the inheritance resolver, a second tenant variant of the mock app, and the
drift dashboard. What is built is the overlay schema (`schema/overlay.py`) -- `replace_target`,
`insert_after`, `skip`, exactly the three operations argued above -- and the layer-hit telemetry
the dashboard would consume, which every real replay in `evidence/` already emits per step. The
resolver that walks a base plus an overlay into a resolved step list, and a second mock-app variant
to run it against, are not built; see Section 7.

---

## 5. Escalation and handoff

**Detecting stuck.** Four triggers, all explicit rather than timeout-based:

- all target layers exhausted for a step
- a checkpoint failed and the observed state matches no `runtime_matches` entry
- a step classified `IRREVERSIBLE` requires human authorisation by policy
- during discovery, N consecutive steps produce no state change

**Routing with context.** An intervention request carries what an operator needs to act without
reconstructing the run: capability and goal, run id, step id, reason code, expected versus
observed, a screenshot, the completed step list, and the session handle. Redaction rules apply to
this payload exactly as they do to logs.

**Taking control of the live session.** The browser is already visible. Automation transitions to
`PAUSED` and stops touching the page; the window, the session cookies and the form state are
untouched at the step where it stopped. The operator works in that window directly and then
signals completion. Nothing is re-launched and nothing is re-entered, which matters because
partially completed work is often not idempotent: re-running a flow that already created a
pending ticket creates a second one.

**Control transfer.** A broker holds the authoritative state. It is a SQLite file, not a service.
Standing up an HTTP broker was considered and rejected: the operator reaches the live session by
clicking the browser window that is already in front of them, not by proxying through the broker,
so the broker's entire job is to hold one row saying who is in control. A process for that is the
scaling infrastructure the brief declines to reward, and it would still leave the claim race
below to be implemented by hand. A file that two processes can open, and that gives conditional
atomic updates for free, is both smaller and more correct.

```
AUTOMATION --stuck--> PAUSED --claim--> HUMAN --release--> RESUMING --> AUTOMATION
                        ^                  |
                        +--- lease expiry -+
```

Three properties, each answering a specific failure:

*Claims are conditional atomic updates, not writes.* `UPDATE control SET state='HUMAN',
holder=? WHERE run_id=? AND state='PAUSED'` and then check the affected row count. Two operators
claiming simultaneously cannot both win, because the second update matches zero rows. A plain
read-modify-write on a JSON file has exactly this race, and it is the reason control state is the
one thing not stored as a file.

*Control is a lease, not ownership.* A holder gets a bounded window and must renew. If an
operator walks away or their process dies, the lease expires and the run returns to `PAUSED` so
someone else can claim it, rather than stranding the run in `HUMAN` forever. Expiry is evaluated
lazily, at read and claim time, not by a background timer: the claim predicate accepts either a
`PAUSED` row or a `HUMAN` row whose lease has already elapsed, so the transition happens in the
same conditional update that grants the new claim.

```sql
UPDATE control SET state='HUMAN', holder=?, lease_expires_at=?
WHERE run_id=?
  AND (state='PAUSED' OR (state='HUMAN' AND lease_expires_at < ?));
```

This matters because the architecture is single-process and synchronous, so a design that
depended on a scheduler to reap leases would either need a component that does not exist or
would fail exactly when that component died. Nothing has to be running for a lease to expire.

*The executor re-checks before every action.* Holding a token is not enough; automation asks the
broker whether it is still the holder immediately before each action, not only once while it is
already blocked waiting on an escalation. This matters before a run has ever gotten stuck too: a
claim racing an in-flight run, or a stale `PAUSED`/`HUMAN` row left over from a reused run_id, both
look identical to "control isn't automation's" from here, and both must stop the run rather than
let it act once more on the strength of a check it did several steps ago. If control changed
mid-flight it stops after at most one action instead of racing the operator's clicks. The cost
is one read per step, which is negligible against the waits already in every step.
`tests/test_escalation_wiring.py::test_control_is_rechecked_before_every_action_not_just_once`
exercises this directly, including the case where control is already lost before step one.

**Resuming.** Control is not handed back to the next step index, because the operator may have
navigated elsewhere. Nor is the current state matched against every checkpoint in the artifact:
checkpoints collide by design, since "on the member search page" is the state before step 1,
after a failed lookup, and after a completed flow, and a scan would have no way to choose among
them. Resume evaluates exactly two candidates, in order:

1. the `success_condition` for the capability. If it holds, the operator finished the work by
   hand. The run returns `SUCCESS` with whatever outputs can be extracted, and no further steps
   execute. Re-running them would repeat work that is often not idempotent.
2. the checkpoint of the step where automation stopped. If it holds, the operator completed that
   step, and execution continues from the next one.

Neither holding is a second escalation rather than a guess. Bounding the search to the step that
was already known plus the terminal condition is what makes resume decidable; a general
"where am I" search is not.

**Session lifetime across a handoff.** Back-office systems commonly expire an idle session in a
few minutes, while routing an intervention, waiting for an operator to pick it up and letting them
read the screen can take longer than that. A design that assumes the session survives the handoff
is assuming away the most likely outcome. Two measures, neither of which needs credentials:

While the run is `PAUSED` and no operator has claimed it, a background thread (`KeepAliveThread`,
started when the intervention is raised and stopped unconditionally when the wait ends) issues a
periodic keep-alive against a route declared `SAFE_READ` in the allowlist specifically for this
purpose. It is a read, it changes no state, and it exists only to stop the idle timer. It runs
against its own out-of-band HTTP client, never the paused page itself, so it can never disturb the
stuck state the operator needs to see. The mock target app doesn't implement session expiry, so
there is no live scenario here that actually needs the ping — it is exercised (the thread does
start and stop, verified in `tests/test_escalation_wiring.py`) but not falsified end to end.

When that is not enough, session loss is a declared outcome rather than a pretence of lossless
resume: a `FailureKind` of `session_lost`, returned with the list of steps that did complete, so
the caller knows what was and was not done. Detected concretely, not assumed: after a handoff
resolves to neither resume candidate holding, if the session's current location has also drifted
outside the allowlisted app scope entirely (the same check that gates every action elsewhere), that
is session loss rather than an ordinary give-up, and is reported as such instead of retried as if
the operator's fix just didn't work. Automatic re-authentication is deliberately not built. It
would require the system to hold credentials, which Section 6 forbids, and it would produce a new
session, which is the one thing the brief's takeover requirement rules out. Reporting honestly
that the session is gone is better than resuming into a session that is not the one the work
started in.

**What is mocked, and why.** The operator console is a CLI (`ops claim <id>`, `ops release <id>`).
The brief permits mocking the operator UI provided the handoff mechanism and control-transfer
model are real, and those are the parts implemented: state machine, conditional claim, lease
expiry, pre-action re-check, checkpoint-based resume.

Also honest about a limit: human actions are recorded as a time window with before/after
screenshots, URL deltas and an operator note, not as a semantic event stream. Capturing raw input
events is feasible, but mapping them reliably back to semantic controls is not a solved problem
at this scope, and claiming otherwise would be claiming more than the code does.

---

## 6. Safety

Four layers, all enforced in the executor rather than trusted to the artifact. An artifact is
data, and data does not get to authorise its own actions.

These are two interception points, not one component. Allowlist and risk-tier checks run between
the decision and the surface, so a refused action is never handed to `act()` at all. Redaction
runs at the persistence boundary, so nothing reaches a log file, a JSONL line or a screenshot
path without passing through it. The positions are not interchangeable: redaction before the
action has nothing to redact, and an allowlist at the write boundary fires after the click has
already happened.

```
decide -> [ allowlist + risk tier ] -> act -> observe
                                                |
                              log / screenshot -+-> [ redaction ] -> evidence/
```

**Allowlist.** Configured domains, route patterns and action types. Anything not listed is
refused. Allowlisting rather than blocklisting, because a blocklist cannot be completed. If a
step names a target outside the allowlist, the step is refused regardless of what the artifact
says, and the refusal is a hard failure with its own code.

**Risk tiers.** Every action carries a level, and the executor gates on it:

| Tier | Examples | Unattended replay |
|---|---|---|
| `SAFE_READ` | read, extract, paginate | allowed |
| `REVERSIBLE_WRITE` | type a search term, switch tab | allowed |
| `IRREVERSIBLE` | submit transfer, close account, change limit | refused by default |

`IRREVERSIBLE` is refused by default and, when enabled in config, requires human authorisation
through the Section 5 path. The brief invites a choice here; default-deny is chosen because the
consequences are asymmetric. A blocked legitimate action costs one operator minute; an
unauthorised irreversible action in a core banking system is not recoverable by retrying. Tiering
is assigned by the executor from the action type and target, not accepted from the artifact,
because otherwise a mislabelled step could downgrade its own risk.

**Approval gate.** `Capability.status` is `draft` or `approved`; replay refuses anything not
`approved` before it ever touches the surface (`FailureDetail.kind == "not_approved"`), the same
default-deny posture as the allowlist above. A fresh discovery run always emits `draft` --
`cua approve <artifact>` is the one-line promotion, a CLI-only stand-in for what a real deployment
would gate on a signed review. This also means an artifact hardening produces is `draft` again even
when its base was `approved`: a new `runtime_match` is new, unreviewed behaviour, and letting it
inherit approval from an unrelated review would make the gate provable but not actually load-bearing.

**Execution bounds.** A loop that never terminates is a guardrail failure, not merely a cost
problem: an unbounded agent loop is repeatedly clicking a real back-office application. Every
loop is bounded by the executor, not by the artifact or the model, with a maximum step count, a
wall-clock ceiling per run, a per-`wait` timeout, a bounded retry count per recoverable
condition, and a no-progress trigger during discovery when N consecutive steps produce no state
change. Hitting any bound is a terminal condition that stops, captures evidence and escalates,
never one that silently keeps going. The bounds live with the allowlist for the same reason the
allowlist is there: a component asking permission to act should not be the component that sets
its own limits.

**Sensitive data.** Two independent mechanisms, because one is not enough:

- *Structural.* Artifacts bind values by reference, so recorded concrete values are absent by
  construction rather than by scrubbing. Nothing to redact is stronger than redacting.
- *Runtime.* The logger redacts on the `sensitivity` tag of any field it writes, and a pattern
  pass catches SSN, PAN and similar shapes in free text. Redaction masks the value and keeps the
  field: `member_id=<pii:5 digits>` rather than a hash or an omission, because a log that cannot
  distinguish two runs of the same capability is not debuggable, and Section 3.5 of the brief
  asks for evidence that explains a run. Correlation is carried by the run id, which is not
  sensitive. Failure screenshots are treated as sensitive artefacts by default: `runs/`, where a
  normal replay writes its evidence, is gitignored wholesale, and a screenshot is never inlined
  into a log line, only referenced by path. The handful of screenshots actually committed under
  `/evidence/` are a deliberate exception for this submission -- they are of the local mock app's
  own generic 500 page, contain no member data, and are exactly the artefacts Section 3.5 asks to
  see evidenced rather than described.

Outputs are tagged too, not just inputs. A returned member name is PII on the way out exactly as
a member id is on the way in. Nothing sets this automatically, though: discovery only ever emits
`sensitivity="none"` on every output it captures, and hardening never revisits it either. `cua
tag-output --artifact <path> --name <output> --sensitivity pii|sensitive` is the human review step
that actually sets it -- the same shape as `cua approve`, and it resets status back to `draft` for
the same reason: it changes what the capability's result contract reveals to a caller, which is
exactly the kind of change the approval gate exists to have a human look at again.

That tagging is only available once a human (or a hardening pass) has actually reviewed an
`OutputSpec` -- and discovery evidence is written before that review has ever happened. A regex
pass alone is not enough there: it catches fixed shapes like an SSN or an account number, but a
plain name matches nothing, and this was verified as a real gap, not a hypothetical one. So a
captured discovery-time output is masked unconditionally wherever it appears in evidence -- the
`outputs` dict, the model's own `finish` tool call, and a `read` step's own observed node text --
the same unconditional treatment `ctx.*` values already get in replay, and for the identical
reason: a value captured live off a screen with nothing yet available to classify it.

**Limits, stated plainly.** The allowlist constrains navigation, not semantics: a step within an
allowed route that does the wrong thing is not caught by it, only by risk tiering and review.
Pattern-based redaction has false negatives on unusual formats. There is no encryption at rest on
the evidence directory. And approval status is a field in the artifact, not an enforced workflow;
a real deployment needs signing and a review gate, which is named in Section 7 rather than built.

---

## 7. Cuts

Everything below is a deliberate omission with the seam left in place, not an unfinished item.

**Cut, with the interface present**

| Cut | Why | What exists |
|---|---|---|
| Desktop and vision adapters | Brief does not ask for them; the argument is the seam, not a second implementation | `SurfaceAdapter` protocol; nothing above it is browser-specific |
| Overlay inheritance resolver | Resolution is mechanical once the schema is right; the schema is the judgement | Overlay schema (`replace_target` / `insert_after` / `skip`); no resolver, no second tenant variant |
| Drift dashboard | Reporting infrastructure, not design | Per-run layer-hit telemetry in the logs |
| Operator web console | Explicitly mockable per the brief | CLI claim/release against the real broker |
| Queues, workers, service split | Explicitly not rewarded; the boundaries are what matter | Four interfaces at the four cut points |
| Postgres / object storage | Files satisfy the reviewability and versioning requirements at this scale | `ArtifactStore` with three methods |

**Cut outright**

- *General conditional branching in the artifact.* A step list cannot express "if the result set
  has more than one row, click one". Adding conditions, jump targets and loop detection would turn
  the artifact from a reviewable list into a small language, and Section 3.2 of the brief wants a
  file a human can approve in a pull request. The position taken instead is that one capability is
  one path: selecting among several matches is a different capability
  (`select_member_from_results`) that a calling agent composes, not a branch inside this one. The
  cases that look like branching and are not are handled already: terminal divergences are
  business outcomes, and optional interstitials are recoverable conditions.
- *Automatic re-authentication after session loss.* Discussed in Section 5. It needs stored
  credentials and produces a different session, so it is reported as an outcome instead.
- *LLM-assisted recovery on replay failure.* Attractive, and a listed stretch goal, but it
  reintroduces a model into the production path. It would need a strict step budget, a policy
  check on the proposed action, and evidence capture before it is safe. Left out rather than
  done loosely.
- *Multi-run flakiness scoring.* Would need enough runs to be meaningful. The mechanism that
  feeds it, layer-hit telemetry, is in place.
- *Semantic capture of human actions during takeover.* Discussed in Section 5.

**Built since the first draft of this list.** *Approval gate.* `Capability.status` was already
`draft | approved`, but replay never checked it -- a draft, unreviewed artifact could run
unattended exactly like an approved one. `replay()` now refuses anything not `approved`
(`FailureDetail.kind == "not_approved"`), and `cua approve <artifact>` is the promotion step, a
CLI-only stand-in for what a real deployment would gate on a signed review. Hardening resets its
output back to `draft` even when the base was approved, since a new `runtime_match` is new,
unreviewed behaviour, not a metadata change -- inheriting approval across that would defeat the
gate's own purpose. `evidence/replay-20260915044628/` was run against a freshly approved artifact,
not a hand-edited one.

**What I would build next, in order**

1. *Overlay resolver plus a drift report.* The point where this stops being a demo and starts
   being operable across institutions.
2. *Desktop adapter against one real legacy application.* The seam is designed for it, but an
   argument is not a test, and this is where the design would actually be falsified.
3. *Bounded assisted recovery,* with the budget and policy checks it requires.
4. *A signed review behind the approval gate above,* rather than a CLI command anyone can run.

**The one thing that is not a cut.** The discovery run is real: a live LLM-driven run against the
mock portal, with the transcript, the emitted artifact and the resulting replay in `/evidence/`.
The brief is unambiguous that this cannot be described in place of being done. Five runs are
committed, indexed in `evidence/README.md`, and every path quoted in this document resolves to a
file in that tree:

| Run | Scenario | Result |
|---|---|---|
| `discovery-20260915043330` | live discovery against the portal, member `12345` (Gemini's daily free-tier quota was exhausted at run time, so `FallbackProvider` fell through to Groq mid-run -- honestly recorded in `run_meta.json`, not silently credited to the configured primary) | emits `acme_core.lookup_savings_balance` v1, status `draft` |
| `replay-20260915044628` | v1 after `cua approve`, a valid member id | `SUCCESS`, typed `Money` output |
| `replay-20260915044639` | v2 (hardened, then approved), an id with no record | `BUSINESS_OUTCOME / MEMBER_NOT_FOUND` |
| `replay-20260915044645` | injected 500 on the entry page | `FAILURE`, with a captured screenshot |
| `demo-handoff-1789447614` | the same injected-500 fault, escalated instead of just failed | `FAILURE` -> operator claims, fixes, releases -> `SUCCESS` on resume |

`demo-handoff-1789447614` is the one worth reading: it exercises checkpoint failure,
classification, evidence capture, the control-transfer state machine and checkpoint-based resume
in a single run.

This is the second generation of this evidence. A review of the first caught two real bugs, not
just wording problems: the compiler could anchor a locator on a dynamic table cell (a results
row's member name ended up recorded in the artifact), and discovery's own CLI wrote the
un-templated goal into its evidence even though `compile_capability` correctly templated it into
the artifact. Both are fixed in `src/cua/agent/compile.py` and `src/cua/cli.py` -- see
`evidence/README.md` for the specifics -- and every file above was regenerated after the fix.
