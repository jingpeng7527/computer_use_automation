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
screen. It also produces the not-found evidence run for free, and `ACCOUNT_FROZEN` /
`PERMISSION_DENIED` the same way from two more member fixtures. The two recoverable conditions
(the session-warning interstitial, the slow load) are the one exception: `cua harden`'s CLI takes
bad *parameters*, and both are query-flag fixtures on the URL rather than something a bad
`member_id` reaches, so they were authored directly as `RuntimeMatch` objects in
`tests/test_recoverable_dialog_e2e.py` rather than mechanically derived -- still real, browser-
verified recoveries, just not run through the hardening pass itself. Beyond what the mock app can
trigger, conditions accumulate through operator escalations, as described in Section 4.

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
The implemented capability's list, after the hardening pass, is `["MEMBER_NOT_FOUND",
"ACCOUNT_FROZEN", "PERMISSION_DENIED"]` — all three observed against the mock portal, member
fixtures `99001` (frozen) and `99002` (above the operator's permission tier) added specifically so
`cua harden` could derive them the same observed-divergence way as `MEMBER_NOT_FOUND`, rather than
hand-authoring the `runtime_match`. Session loss during a handoff is a different case, and is
implemented: it is a
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
recording is structurally absent from the file. Not just asserted here --
`tests/test_no_recording_time_literals_in_artifacts.py` JSON-scans every artifact actually
committed to this repo (every `steps`/`summary`/`goal`/`provenance` field, not only the locator a
single compiler function targets) for the recording-time member id, name and balance, so a future
regression anywhere in the compile/save path fails a test, not just a manual grep. Discovery
itself is testable the same way, with no API key or browser: `run_discovery()` and
`compile_capability()` both take plain data (`LLMProvider`/`SurfaceAdapter` protocols, a
`DiscoveryTranscript`), so `tests/test_discovery_compile_replay_with_fake_provider.py` drives a
scripted provider and a scripted surface through discover -> compile -> replay end to end,
asserting the same no-literal-leak property on a freshly compiled artifact and that it actually
replays against a fresh instance of the same fake surface.

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

Recovery gets an additional, narrower guard: every `recovery.do` value re-performs the step's own
action at the end of `_apply_match` -- `dismiss_dialog` and `reload` do something extra FIRST, but
all four fall through to the identical retry -- and a failed checkpoint does not prove that action
didn't already take effect: a slow POST looks identical to one that never fired. Retrying a
`Click`, `TypeText` or `Select` step risks performing it twice (a double-submitted payment)
regardless of which `do` got it there, so any recoverable match on one of those three is refused,
checked both at artifact-validation time (when the step is named statically by `after_step`) and
again at replay time as a backstop for an `after_step: null` matcher, which the schema can't check
statically since it could land on any step. (This guard originally keyed on `do == "retry_step"`
specifically -- a real gap, since `dismiss_dialog`/`reload` on a Click step carried the identical
risk and sailed through both layers unchecked; see Section 7.) The check reads the action's own
discriminated `type`, never `Step.risk_level` -- that field is the same self-reported hint Section 6
already refuses to trust for risk tiering, and a hand-edited artifact declaring `risk_level:
SAFE_READ` on a `Click` step would sail past a check keyed on it. `IRREVERSIBLE` needs no separate
case: a retry recurses into the same step-execution path every attempt already goes through, so the
ordinary risk gate refuses or escalates it before a checkpoint can even fail a second time -- there
is no point at which any recovery on an `IRREVERSIBLE` step is ever actually reached.

Both layers have real evidence, not just unit tests, though the schema-level one necessarily
does (there is no "live" version of an artifact that was refused before it could exist):
`evidence/recovery-refused-schema-*/validation_error.txt` is the actual `pydantic.ValidationError`
raised attempting to construct one. The runtime backstop's evidence,
`evidence/recovery-refused-runtime-*/result.json`, runs the real `replay()` engine against a
scripted surface (the mock target app has no fixture that can genuinely get a `REVERSIBLE_WRITE`
step stuck behind a dialog); its `NOTE.md` says exactly that, and confirms `click_count == 1` --
the guard refuses before a second click, not after one already landed twice.

This class also absorbs what looks like a need for conditional branching. "A compliance notice
appears on the first login of the month and not otherwise" needs no branch: it is a recoverable
matcher, so it is dismissed when present and never matches when absent. Optional interstitials
are a recovery concern, not a control-flow concern, and this is the main reason the category
exists.

**Drift, secondarily.** Because the underlying UIs change slowly, drift is treated as a signal to
collect rather than a condition to recover from at runtime. Each resolution records the layer
that succeeded (`StepResult.locator_layer_hit`). The system does not attempt to self-heal a
changed UI during a replay; silently adapting is exactly the non-determinism the artifact exists
to remove.

`cua drift-report` (`src/cua/observability/drift.py`) turns that per-run recording into the
actual cross-run signal: it scans every `evidence/*/result.json`, groups `locator_layer_hit` by
`(capability_id, step_id)` in the order those runs really happened (`started_at`, not directory or
run-id string order, which a hand-timestamped run_id and a start time can disagree on), and
classifies the trend. A single deeper hit is `occasional` -- a locator can miss a layer once on a
slow paint and resolve fine the next run, and flagging that identically to a real change would
train whoever reads the report to ignore it. `drifting` requires the last `persistence_window`
(default 3) runs of the same step to ALL land deeper than where that step first resolved --
`tests/test_layer_drift.py` has a case for each status, including the one that makes the
distinction matter: a lone deeper hit sandwiched between two baseline ones stays `stable`, not
`drifting`. Run live against every real evidence file committed to this repo, it reports
`[stable]` on all nine tracked steps, correctly, since none of them has yet seen a real repeated
markup change across runs -- an honest negative, not a fabricated positive.

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
rather than only the successful one. Four member records cover the outcomes (present `12345`,
absent `99999`, frozen `99001`, above the operator's permission tier `99002`); query flags on
`/members/search` inject a session-warning interstitial (`?interstitial=1`), a slow load
(`?slow=1`) and a server error (`?inject=500`); and one control on the detail screen is an
irreversible action, so the risk tier in Section 6 can be shown refusing rather than described.
A "compliance notice" was considered as a second, separately-worded interstitial and deliberately
not built: it is the identical `dismiss_dialog`/optional-interstitial mechanism as the
session-warning one, just different copy, and a second fixture for the same mechanism would
demonstrate nothing the first doesn't already. Being able to trigger these on demand is the whole
reason the target is local: a
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
version: 2

# tenant overlay, stores differences only
extends: "acme_core.lookup_savings_balance@2"
tenant_id: "cu_northgate"
allowed_route_patterns: ["/tenant-b/*"]   # this tenant's own declared scope (Section 6)
add_inputs:
  - { name: branch, type: string, required: true }   # this tenant has a mandatory branch selector
overrides:
  - op: replace_value          # this tenant's entry point is a different route entirely
    step_id: "s0"
    value: "http://127.0.0.1:8800/tenant-b/members/search"
  - op: insert_after            # the branch selector itself
    step_id: "s0"
    step: { id: "s0b", action: { type: select, value_from: "{{input.branch}}", by: value }, ... }
  - op: replace_target
    step_id: "s1"
    target:
      strategies:
        - { type: label_anchor, label: "Acct/Member #:", relation: same_row_input }
  - op: replace_target          # a differently-classed control, still no role/name
    step_id: "s3"
    target: { strategies: [{ type: css, selector: ".detail-link" }] }
  - op: replace_target          # the balance row label is abbreviated on this build
    step_id: "s4"
    target: { strategies: [{ type: label_anchor, label: "SAV BAL", relation: next_cell }] }
```

Four operations, not one, because real tenant divergence is not only relabelling. Institutions
running the same product add a required field, point an entry step at a different route, or omit a
confirmation screen, and an overlay that can only rewrite a target cannot express any of the last
three. `replace_value` exists specifically for the entry-route case above: a `Navigate` step's own
literal URL is not a target (a navigate step has nothing to locate on the page), so overriding it
needed its own operation.

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

**This is demonstrated, not asserted.** `apply_overlay()` (`src/cua/overlay/resolver.py`) resolves
a base capability plus the YAML-equivalent JSON above into a concrete, replayable capability; the
resolved artifact is then validated by round-tripping through `Capability.model_validate()` rather
than by a second copy of the reference-integrity logic, so every invariant Section 2 already
enforces on a base capability -- no duplicate step ids, no dangling `input.*`/`ctx.*`/`output.*`
reference, `RuntimeMatch.after_step` naming a real step -- is re-checked against the *resolved*
step list for free. `tests/test_overlay_resolver.py` exercises all four operations plus both
rejection paths (a wrong `extends` version; a `skip` that breaks a downstream `ctx.*` reference).

The mock target app now ships a genuine second tenant: `cu_northgate`, mounted at `/tenant-b/...`,
same underlying data, deliberately different markup wherever a real older build of the same
product would diverge -- a different label on the member-id field, an entirely different (and, in
the live browser, actually `required`) branch selector the base tenant's build doesn't have at
all, a differently-classed detail-view control, and an abbreviated balance-row label. Deliberately
*unchanged*: the "Search" button's accessible name and the "Member Detail" heading, both of which
survive across real UI builds, so the overlay overrides five things and leaves two of the base
artifact's five steps completely untouched. `evidence/replay-20260915224332/` and
`evidence/replay-20260915224345/` are the same resolved capability -- literally the same JSON file
-- correctly returning `SUCCESS` with a typed `Money` output and `BUSINESS_OUTCOME /
MEMBER_NOT_FOUND` against that second, genuinely different surface, with no override needed for
the outcome detection at all (its wording happens to match on both builds, so nothing had to name
it in the overlay). The overlay also declares this tenant's own scope
(`allowed_route_patterns: ["/tenant-b/*"]`, Section 6) -- verified directly, not just declared:
pointing a copy of the same resolved artifact's entry step at the base tenant's `/members/search`
instead is refused with `policy_blocked`, naming the declared pattern that excluded it, even
though that route is well within `policy.yaml`'s own global allowlist.

Not cut, in the end: `cua drift-report` (Section 3) aggregates exactly this telemetry -- across
tenants too, since it groups by `capability_id`, and `acme_core.lookup_savings_balance` and its
`cu_northgate` overlay resolution are different capability ids in the same `evidence/` tree. What
is still not built is alerting on a threshold crossing in CI; the command's own non-zero exit code
on a `drifting` verdict is the hook a CI step would need, just not wired to one yet.

---

## 5. Escalation and handoff

**Detecting stuck.** Four triggers, all explicit rather than timeout-based:

- all target layers exhausted for a step
- a checkpoint failed and the observed state matches no `runtime_matches` entry
- a step classified `IRREVERSIBLE` requires human authorisation by policy
- during discovery, N consecutive steps produce no state change

The first three are replay-side and share one mechanism, covered first below. The fourth is
discovery-side and needed a genuinely different one -- covered on its own, after, because
"replay's handoff, reused as-is" would have produced a broken artifact (see that section for why).

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
stuck state the operator needs to see.

A real bug lived here until the thread actually started firing: `KeepAliveThread` was constructed
with the caller's own `ControlBroker` (and the sqlite3 connection inside it), but that connection
was opened on the main thread, and sqlite3 connections are only usable from the thread that opened
them by default. The class existed since the escalation mechanism was first built, but nothing
called `.start()` on it until the wait loop was wired up -- so the cross-thread misuse had no live
path to fire and went unnoticed until the very first real `PAUSED` wait that outlived one
`keep_alive.interval_s` tick, which raised `sqlite3.ProgrammingError` from inside the background
thread, silently (Python's default thread excepthook only prints an unhandled exception, it does
not propagate to or stop the main thread -- the escalation wait kept running with a dead keep-alive
underneath it). Fixed by giving the thread a `db_path` instead of a broker instance and having it
open its own connection inside `run()`, the standard shape for using sqlite3 across threads.
`tests/test_keepalive_thread_safety.py` reproduces the original crash against the pre-fix code (a
shared connection raising from the background thread) and locks in the fix; a second, unrelated
bug found while writing that reproduction -- the class stored its stop `Event` as `self._stop`,
silently shadowing `threading.Thread`'s own private `_stop()` method that `.join()` relies on
internally -- is fixed alongside it.

The mock target app doesn't implement session expiry, so
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
started in. `evidence/session-lost-20260915234907/` runs this for real: the actual `replay()`
engine, a real SQLite `ControlBroker` with two independent connections claiming and releasing
across a real thread boundary (mirroring the two real OS processes `cua replay`/`cua ops` are in
production), and a real operator note left on release -- only the `SurfaceAdapter` is scripted,
since the mock app still has no session-expiry mechanism to trigger this against a real page.
`result.json`'s `failure.observed` (`"now at http://evil.example.com/login"`) and its
`human_action.json` (`resume_decision: "none"`, disagreeing with the release note, same point as
`replay-20260915230202/` above) are both genuine output of that real code, not hand-written.

**What is mocked, and why.** The operator console is a CLI (`ops claim <id>`, `ops release <id>`).
The brief permits mocking the operator UI provided the handoff mechanism and control-transfer
model are real, and those are the parts implemented: state machine, conditional claim, lease
expiry, pre-action re-check, checkpoint-based resume.

**What the operator actually did.** Every handoff writes `human_action.json` alongside
`intervention.json`: who claimed it and when, the before/after URL and screenshot, the operator's
own free-text note (`cua ops release --note "..."`), and two further fields that are deliberately
*not* the same thing -- `resume_decision` (`"success"` / `"step"` / `"none"`, the literal value
`find_resume_point` returned) and `human_performed_pending_action` (`resume_decision in ("success",
"step")`). Both are derived mechanically by re-checking the actual page against the stuck step's
own checkpoint, exactly as resume already does -- never from the operator's self-report, and never
from an artifact-declared field, for the same reason risk is never trusted from `Step.risk_level`
(Section 6): whether the fix worked is a fact about the live session, not something anyone gets to
assert. The operator's note is kept, but kept separately, labelled as an account rather than a
fact. `evidence/replay-20260915230202/human_action.json` is a real run of this against the live
app and a real SQLite broker, deliberately releasing control *without* actually fixing the fault
(`--note "checked the page, fault still present..."`) specifically to show the derived field
disagreeing with a well-intentioned note: `resume_decision: "none"`, `human_performed_pending_action:
false`, even though a note was left. `tests/test_human_action_evidence.py` covers the true branch
against a scripted surface, since forcing a *successful* fix through this same headed-browser
session from a test process would require driving the one live page from two threads at once,
which Playwright's sync API does not allow -- the same reason a real operator uses the visible
window rather than a script in the first place.

Also honest about a limit: human actions are recorded as a time window with before/after
screenshots, URL deltas and an operator note, not as a semantic event stream. Capturing raw input
events is feasible, but mapping them reliably back to semantic controls is not a solved problem
at this scope, and claiming otherwise would be claiming more than the code does.

**Discovery-side handoff -- a different mechanism, not replay's reused.** The fourth trigger above
looks like it should be the same problem as the first three: automation is stuck, a human clears
it, control comes back. Reusing replay's handoff as-is would be wrong in a way that only shows up
once compiled: `StepLog` only ever records an LLM tool call, so if a human's own clicks resolved
the stall, whatever they did is invisible to `compile_capability()` -- the resulting artifact would
be missing exactly the steps that got it unstuck, and would fail the moment anyone replayed it
unattended. The fix is not a variant of resume; it's a narrower contract for what a discovery
handoff is even allowed to produce.

*The operator declares a structured resolution, not a fact.* Unlike replay, discovery has no
compiled checkpoint yet to mechanically re-derive a resume point from -- there is nothing here
playing `find_resume_point`'s role. So the person holding the lease must say, in one of exactly two
words, what happened:

- `cleared_obstacle` -- a transient obstacle (an unexpected dialog, a stuck load) is gone. Control
  returns to the LLM with a FRESH observation; the loop continues, and only what the LLM decides
  from here becomes a `StepLog`.
- `workflow_advanced` -- the operator did some or all of the actual task by hand. Whatever the LLM
  had recorded up to this point cannot become an artifact, because the steps that mattered were
  never logged. The run aborts with `success=False` -- `compile_capability()`'s existing "cannot
  compile a failed discovery run" refusal is what actually prevents an artifact here; no second,
  parallel way to say "no artifact" was added for this case.

`ControlBroker.release()` enforces this itself (`ControlRow.phase`, written by `mark_stuck()`, never
by whoever calls release), not the CLI: a replay-phase intervention takes no `--resolution` at all
and a discovery-phase one requires one of exactly the two values above, or the broker raises before
the state transition happens. Trusting the CLI layer alone would mean any other caller of the same
broker could bypass the distinction; enforcing it in the one place every caller has to go through
does not.

*Budgets are layered, and only one layer resets.* `ExecutionGuard.reset_progress()` clears the
no-progress streak and reseeds it with the real post-handoff observation -- nothing else. `max_steps`
and the wall-clock ceiling have no reset path at all (by omission, not an added check: those
counters simply never expose one), and a SEPARATE bound,
`policy.execution_bounds.max_discovery_handoffs` (default 1, consumed by
`ExecutionGuard.use_discovery_handoff()`), caps how many times one run may hand off at all --
otherwise a genuinely confused LLM and a patient operator could hand off forever without ever
touching `max_steps`.

*What the human did is evidence, never an artifact input, here too.* Same shape as replay's
`human_action.json` (before/after URL and screenshot, the operator's own note, `same_session:
true`) plus the one field replay's version doesn't need: `resolution` itself, recorded as the
structured command it is -- never promoted to a derived "fact" the way
`human_action.human_performed_pending_action` is, because there is no checkpoint here that could
derive one.

*A handoff artifact cannot be approved directly.* `Provenance.discovery_handoffs` (incremented by
`compile_capability()`, once per `cleared_obstacle` intervention in the transcript) is what `cua
approve` checks. Nonzero, and it refuses outright: the recorded LLM steps ran partly on a page a
human already reached into, and nothing has yet proven they hold up completely unattended.
`cua validate` is the one way to produce that proof -- it replays the artifact from a FRESH browser
session with `status` flipped to `"approved"` only in memory (never written to disk; `replay()`
itself is neither modified nor bypassed, so a real `cua replay` against the same file still refuses
it) and with escalation hard-disabled, not merely defaulted off: getting stuck during validation is
what validation exists to catch, not a second chance at a handoff. A pass writes
`evidence/<run_id>/validation.json`, carrying an `approval_snapshot_sha256` -- the whole artifact
hashed, deliberately excluding only `status` rather than a hand-picked list of "fields that affect
execution" (that list is a judgment call with real room to leave one out; hashing everything else
costs nothing but an occasional redundant re-validation). `cua approve --validation-run <id>`
recomputes that hash against the artifact it is about to promote and refuses on any mismatch, so an
edit made after validation -- even one that looks cosmetic -- invalidates the proof rather than
silently riding through on a stale evidence file.

`tests/test_discovery_handoff.py` exercises the full state machine (a real `ControlBroker` against a
temp SQLite file, a scripted LLM provider and surface, no browser) for both resolutions and for the
no-broker case unchanged; `tests/test_execution_guard.py` covers the budget layering directly;
`tests/test_validate_approve_gate.py` runs `cua validate` then `cua approve --validation-run`
against the real target app and a real browser, including the tamper case (an edit between
validation and approval is refused, not silently accepted).

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

A capability can additionally declare its own expected scope --
`AppProfile.base_url` / `allowed_route_patterns` -- and the executor takes the intersection:
`effective scope = policy.yaml's allowlist ∩ the capability's own declared scope ∩ any tenant
override's scope`. This is a narrowing, never a grant: an artifact with neither field set is
scoped by `policy.yaml` alone, exactly as before this existed, and one that declares a scope
`policy.yaml` doesn't already permit stays refused (checked against the global allowlist first).
The point is reviewability, not a second security boundary -- a human approving a capability can
see, in the one file they're reviewing, exactly which routes it's meant to touch, instead of
cross-referencing `policy.yaml` to infer it. It also gives multi-tenant reuse a real enforcement
hook: `cu_northgate`'s overlay declares `allowed_route_patterns: ["/tenant-b/*"]`, so that resolved
capability cannot wander into the base tenant's `/members/*` routes even though both are inside
the same globally-allowed origin -- verified directly: pointing a copy of it at `/members/search`
is refused with `policy_blocked`, naming the declared pattern that excluded it, not the global
allowlist (which would have allowed that path fine). `cua set-scope` is the CLI command that
declares this on an artifact; like `cua tag-output`, it resets status to `draft`, since it changes
the enforced boundary the capability runs inside.

A known sharp edge, fixed rather than left as a footnote: `cua set-scope` originally had no
pre-flight of its own -- it would happily save a scope that already excluded one of the
capability's own steps, and the mistake would only surface later, mid-replay, as `policy_blocked`
on whichever step hit it first. Fail-safe (nothing was ever wrongly *allowed*), but not fail-loud
(the mistake wasn't reported where it was made). The command now checks every step whose action is
a Navigate with a literal (non-templated) URL against the scope it's about to save, using the same
`check_app_profile_scope` replay itself calls, and refuses -- unless `--force` -- if any would
already be excluded. Necessarily partial: a step that reaches a page by clicking a link (this
project's own capability does exactly that for its detail page) has no literal URL recorded in the
artifact to check at all; that class of mistake still only surfaces at replay time.
`tests/test_set_scope_preflight.py` covers refuse / `--force` / already-consistent / can't-check-a-
template, and it's verified live: pointing `cua set-scope` at the real
`acme_core.lookup_savings_balance` artifact with `--allowed-route-pattern "/tenant-b/*"` refuses
immediately, naming step `s0`'s own entry URL as the conflict, rather than saving silently and
waiting for the next replay to fail.

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
`evidence/replay-20260915235033/` is this refused live, on the real, currently-approved
`acme_core.lookup_savings_balance` v2 artifact: `cua set-scope` was run with its own existing
values (which still resets status to `draft`, correctly -- any change to the enforced boundary
needs a fresh review even if the values didn't actually move), then `cua replay` against that
now-draft artifact refused in 0.37s, before the browser was ever touched. `cua approve` was run
immediately after to restore it; the artifact's `git diff` is empty.

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

**Built since the first draft of this list.** *Approval gate.* `Capability.status` was already
`draft | approved`, but replay never checked it -- a draft, unreviewed artifact could run
unattended exactly like an approved one. `replay()` now refuses anything not `approved`
(`FailureDetail.kind == "not_approved"`), and `cua approve <artifact>` is the promotion step, a
CLI-only stand-in for what a real deployment would gate on a signed review. Hardening resets its
output back to `draft` even when the base was approved, since a new `runtime_match` is new,
unreviewed behaviour, not a metadata change -- inheriting approval across that would defeat the
gate's own purpose. `evidence/replay-20260915044628/` was run against a freshly approved artifact,
not a hand-edited one.

*Overlay resolver and a second tenant.* Section 4 covers this one in full -- `apply_overlay()`, a
fourth override op (`replace_value`) the original three couldn't express, a genuine second tenant
mock (`/tenant-b/...`), and both `SUCCESS` and `BUSINESS_OUTCOME` demonstrated against it with the
same resolved artifact.

*Layer-hit drift aggregation.* `cua drift-report` (Section 3) also turned out buildable in scope:
it aggregates the per-run `locator_layer_hit` telemetry every replay already emits, across
capabilities and tenants alike, and distinguishes a persistent demotion (`drifting`) from a
one-off blip (`occasional`) rather than firing on both identically.

*Semantic capture of human actions during takeover.* Not a cut after all -- built, and belongs
here instead of the list above. Every handoff writes `human_action.json` alongside
`intervention.json`: before/after URL and screenshot, the operator's own release note, and
`human_performed_pending_action`, a fact `find_resume_point` derives mechanically rather than
takes on the operator's word (`evidence/replay-20260915230202/` is a real run where a good-faith
note and the derived fact disagree). Section 5 covers it in full; this Cuts list had simply not
been updated after it landed.

*A retry-safety guard gap, found auditing the guard itself rather than by comparison.* Both the
schema-level check (`Capability._referential_integrity`) and the replay-time backstop for
`recovery.do == "retry_step"` on a Click/TypeText/Select step originally checked that one `do`
value specifically. `dismiss_dialog` and `reload` fall through to the identical retry at the
bottom of `_apply_match`, so a hardened artifact declaring either of those on a non-idempotent
step carried the same double-submit risk unguarded. Both layers now key on any recoverable match
against Click/TypeText/Select, not on which `do` was declared.

*Two declared-but-unenforced schema fields, closed.* `ParamSpec.enum_values` existed since Phase A
but `_validate_params` only ever checked `pattern` -- an enum-typed input with no separately
authored regex (the common case, since `enum_values` was assumed to be the check) accepted
anything. Now enforced before replay touches the surface. `Condition`'s `NamedPredicate` escape
hatch was never wired up in `evaluate_condition` (unconditionally `False`), which meant it was
possible to build a `Checkpoint` that could never pass -- structurally valid, silently
unsatisfiable. Removed from the union rather than left half-built; it returns once a real branch
backs it.

*Discovery-side handoff.* Listed in Section 5's own "Detecting stuck" trigger table since an early
draft -- `guard.check_progress()` (agent/loop.py) had always raised `BoundExceeded` on a no-progress
stall, but nothing caught it: the CLI's `discover` command only ever handled `ValueError`, so a
stall propagated out as an unhandled exception and the browser closed in `finally`. Genuinely
harder than "reuse replay's handoff," not merely unwired: discovery has no compiled checkpoint to
derive a resume point from, and a human's own clicks during a handoff would otherwise vanish from
the artifact entirely (`StepLog` only records LLM tool calls). Built as its own mechanism --
`resolution` as a structured, broker-enforced operator command
(`cleared_obstacle`/`workflow_advanced`), a separate `max_discovery_handoffs` budget, and a
`cua validate` / `cua approve --validation-run` gate proving a handoff-touched artifact still
replays clean, unattended, from a fresh session before it can be promoted. Section 5 covers it in
full.

**What I would build next, in order**

1. *Wire `cua drift-report`'s exit code into CI,* so a `drifting` verdict actually blocks a merge
   instead of requiring someone to run the command by hand.
2. *Desktop adapter against one real legacy application.* The seam is designed for it, but an
   argument is not a test, and this is where the design would actually be falsified.
3. *Bounded assisted recovery,* with the budget and policy checks it requires.
4. *A signed review behind the approval gate above,* rather than a CLI command anyone can run.

**The one thing that is not a cut.** The discovery run is real: a live LLM-driven run against the
mock portal, with the transcript, the emitted artifact and the resulting replay in `/evidence/`.
The brief is unambiguous that this cannot be described in place of being done. Twelve runs are
committed, indexed in `evidence/README.md`, and every path quoted in this document resolves to a
file in that tree:

| Run | Scenario | Result |
|---|---|---|
| `discovery-20260915043330` | live discovery against the portal, member `12345` (Gemini's daily free-tier quota was exhausted at run time, so `FallbackProvider` fell through to Groq mid-run -- honestly recorded in `run_meta.json`, not silently credited to the configured primary) | emits `acme_core.lookup_savings_balance` v1, status `draft` |
| `replay-20260915044628` | v1 after `cua approve`, a valid member id | `SUCCESS`, typed `Money` output |
| `replay-20260915044639` | v2 (hardened, then approved), an id with no record | `BUSINESS_OUTCOME / MEMBER_NOT_FOUND` |
| `replay-20260915044645` | injected 500 on the entry page | `FAILURE`, with a captured screenshot |
| `demo-handoff-1789447614` | the same injected-500 fault, escalated instead of just failed | `FAILURE` -> operator claims, fixes, releases -> `SUCCESS` on resume |
| `replay-20260915224332` | the `cu_northgate` overlay resolved from v2 and approved, replayed against the SECOND tenant's mock app, a valid member id | `SUCCESS`, same typed `Money` output, on a genuinely different surface |
| `replay-20260915224345` | same resolved tenant artifact, an id with no record | `BUSINESS_OUTCOME / MEMBER_NOT_FOUND`, with no outcome-detection override needed |
| `replay-20260915230202` | same injected-500 fault, escalated; operator claims and releases WITHOUT fixing anything (`--note "checked the page, fault still present..."`) | `FAILURE` on resume, `human_action.json` records `resume_decision: "none"`, `human_performed_pending_action: false` -- the derived fact disagreeing with a good-faith note is the point of this run |
| `replay-20260915235033` | the real, approved v2 artifact, reset to `draft` via `cua set-scope` (its own existing values -- see the row's `NOTE.md`), then replayed | `FAILURE`, kind `not_approved`, in 0.37s -- refused before the browser is touched. `cua approve` restored v2 immediately after; its `git diff` is empty |
| `recovery-refused-schema-20260915234907` | attempted to construct a `Capability` declaring `retry_step` on a `Click` step | Real `pydantic.ValidationError` -- **scripted, no browser**, see `NOTE.md` |
| `recovery-refused-runtime-20260915234907` | real `replay()`, same class of artifact but with `after_step: null` (the shape the schema check can't catch statically) | `FAILURE`, kind `recovery_refused`, `click_count == 1` -- **scripted surface**, see `NOTE.md` |
| `session-lost-20260915234907` | real `replay()` + a real SQLite broker across a real thread boundary; operator claims, releases without fixing anything, on a surface reporting an off-scope location | `FAILURE`, kind `session_lost` -- **scripted surface**, see `NOTE.md` |

`demo-handoff-1789447614` is the one worth reading for the handoff mechanism; the two
`cu_northgate` runs are the one worth reading for Section 4's multi-tenant claim, since they are
the same base capability resolved once and replayed against a surface it was never recorded on;
`replay-20260915230202` is the one worth reading for the `human_action.json` evidence above. The
last four rows exist for a narrower reason: `not_approved`, `recovery_refused` and `session_lost`
were all real, already-implemented, already-unit-tested mechanisms with no evidence proving they
actually fire outside a test file -- a gap found by re-reading the brief's own evidence
requirement, not a bug in the mechanisms themselves. Two of the four necessarily use a scripted
surface rather than the real mock app, and say so plainly in their own `NOTE.md`.

This is the second generation of this evidence. A review of the first caught two real bugs, not
just wording problems: the compiler could anchor a locator on a dynamic table cell (a results
row's member name ended up recorded in the artifact), and discovery's own CLI wrote the
un-templated goal into its evidence even though `compile_capability` correctly templated it into
the artifact. Both are fixed in `src/cua/agent/compile.py` and `src/cua/cli.py` -- see
`evidence/README.md` for the specifics -- and every file above was regenerated after the fix.
