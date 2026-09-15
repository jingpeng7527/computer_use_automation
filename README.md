# computer-use automation

An LLM discovers how to complete a goal inside a legacy back-office UI once. The run
is recorded as a typed, versioned **capability artifact**. From then on, a
**deterministic replay engine** re-runs that flow with no LLM in the decision loop,
returns typed outputs, and separates expected business outcomes from recoverable
conditions and hard failures. When it cannot safely proceed, it escalates to a human
who takes control of the same live session and hands it back.

See `REPORT.md` for the full design rationale (architecture, schema, error handling,
multi-tenant story, escalation, safety, cuts). See `evidence/README.md` for an index
of every committed run and the exact command that produced it.

## Layout

| Path | What |
| --- | --- |
| `src/cua/target_app/` | Local mock legacy credit-union servicing console (the target surface) |
| `src/cua/surface/` | Surface abstraction: perceive/act seam (Playwright web impl) |
| `src/cua/schema/` | Capability artifact schema + result contract (Pydantic) |
| `src/cua/agent/` | LLM discovery loop (observe -> decide -> act), Gemini + Groq fallback |
| `src/cua/replay/` | Deterministic replay engine + error taxonomy |
| `src/cua/safety/` | Allowlist, risk tiers, execution bounds, redaction |
| `src/cua/escalation/` | Stuck detection, SQLite control-transfer broker, `ops` CLI |
| `src/cua/overlay/` | Multi-tenant overlay resolution (`apply_overlay`) |
| `docs/schema/` | Generated Pydantic JSON Schema, DOT, and reviewable SVG contract diagrams |
| `config/policy.yaml` | Allowlist, risk tiers, redaction rules, execution bounds |
| `artifacts/` | Saved capability artifacts (one JSON file per version) |
| `overlays/` | Reviewable tenant overlay deltas (input to `cua overlay apply`) |
| `evidence/` | Committed evidence from real discovery + replay runs |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium
cp .env.example .env
```

Fill in `.env`:

- `GEMINI_API_KEY` -- required for a discovery run. Free tier: https://aistudio.google.com/apikey
- `GROQ_API_KEY` -- optional secondary provider. If the Gemini call itself fails
  (network error, rate limit), discovery falls back to Groq instead of aborting the
  run. Leave unset to run on Gemini alone. Free tier: https://console.groq.com/keys

**Replay, hardening, and everyday operation need no API key and call no LLM.** Only
`cua discover` ever talks to a model.

## Demo path

Run these in order, each in its own terminal where noted.

```bash
# 1. start the target app (leave running)
.venv/bin/cua serve-target

# 2. discovery: a live LLM run against the app -> emits a capability artifact
#    (opens a real, visible browser window -- this is not mocked)
.venv/bin/cua discover \
  --goal "look up member 12345 and read their current savings balance" \
  --target http://127.0.0.1:8800/members/search \
  --name lookup_savings_balance
# -> artifacts/acme_core.lookup_savings_balance/1.json (status: "draft")
# -> evidence/discovery-<timestamp>/{transcript.json, run_meta.json, artifact_emitted.json}

# 3. approve: unattended replay refuses anything not reviewed -- a fresh
#    discovery emits "draft" on purpose, and this is the one-line promotion
#    a real review would gate (REPORT.md sec 7). Without this, step 4 below
#    fails with status: failed, kind: not_approved.
.venv/bin/cua approve --artifact artifacts/acme_core.lookup_savings_balance/1.json

# 4. scope: declare what THIS capability is allowed to touch, narrower than
#    policy.yaml's own global allowlist (REPORT.md sec 6). This is a ceiling,
#    not a grant -- it can only shrink the effective allowlist, never widen it.
.venv/bin/cua set-scope \
  --artifact artifacts/acme_core.lookup_savings_balance/1.json \
  --base-url http://127.0.0.1:8800 \
  --allowed-route-pattern "/members/*"
# -> resets status to "draft" (a scope change is a reviewable change); approve again
.venv/bin/cua approve --artifact artifacts/acme_core.lookup_savings_balance/1.json

# 5. replay: deterministic re-run of that artifact, no LLM, valid input
.venv/bin/cua replay \
  --artifact artifacts/acme_core.lookup_savings_balance/1.json \
  -p member_id=12345
# -> status: success, typed Money output

# 6. hardening: observe a real failure mode (no LLM) and bake it into the artifact
#    as a runtime_match, so replay can classify it as a business outcome next time.
#    A hardened artifact is new, unreviewed behaviour, so it comes back "draft"
#    even though its base was approved -- approve it again before replaying it.
.venv/bin/cua harden \
  --artifact artifacts/acme_core.lookup_savings_balance/1.json \
  -p member_id=99999
# prints the real page text seen after the bad lookup; re-run with:
.venv/bin/cua harden \
  --artifact artifacts/acme_core.lookup_savings_balance/1.json \
  -p member_id=99999 \
  --detect-text "No member records match" \
  --outcome-code MEMBER_NOT_FOUND
# -> artifacts/acme_core.lookup_savings_balance/2.json (status: "draft", inherits scope)
.venv/bin/cua approve --artifact artifacts/acme_core.lookup_savings_balance/2.json

# 7. replay against the hardened artifact with input that hits that outcome
.venv/bin/cua replay \
  --artifact artifacts/acme_core.lookup_savings_balance/2.json \
  -p member_id=99999
# -> status: business_outcome, code MEMBER_NOT_FOUND (not a crash)

# 8. escalation and handoff: force a failure, take control of the same live
#    browser session, fix it by hand, hand back, watch replay resume and finish
.venv/bin/cua replay \
  --artifact artifacts/acme_core.lookup_savings_balance/2.json \
  -p member_id=12345 \
  --fault "inject=500"
# stalls with: "if this run gets stuck: cua ops claim <run_id> ..."
# in a second terminal, once it's stuck:
.venv/bin/cua ops claim <run_id>
#   -> fix it by hand in the already-open browser window (e.g. reload without ?inject=500)
.venv/bin/cua ops release <run_id> --note "reloaded without ?inject=500"
# the waiting replay process notices, re-checks its checkpoint, and resumes to SUCCESS
# -> evidence/<run_id>/human_action.json records what actually happened: before/after
#    URL + screenshot, your note, and a MECHANICALLY DERIVED resume_decision /
#    human_performed_pending_action -- from re-checking the real page, not from the
#    note (REPORT.md sec 5). See evidence/replay-20260915230202/ for a run where a
#    release note claimed a fix that hadn't actually happened, and the derived field
#    said so.
```

`--fault` is a dev/demo hook only: it appends a query string to the first navigate
step for that one run, to reproduce an error scenario without editing the saved
artifact. It never short-circuits the engine -- replay still observes the real page
and classifies whatever it actually sees; `evidence/README.md` explains how to check
that for yourself.

## Multi-tenant overlay demo

The same base capability, replayed against a genuinely different second tenant's
markup (REPORT.md sec 4) -- different field labels, a real `required` branch
selector the base tenant's build doesn't have, a differently-classed detail
control, an abbreviated balance label. Requires `cua serve-target` running (it
mounts both tenants).

```bash
# resolve the tenant delta against the hardened v2 artifact -> a new,
# tenant-specific capability (status: "draft" -- new composition, review it)
.venv/bin/cua overlay apply \
  --base artifacts/acme_core.lookup_savings_balance/2.json \
  --overlay overlays/acme_core.lookup_savings_balance/cu_northgate.json
# -> artifacts/acme_core.lookup_savings_balance.cu_northgate/1.json

.venv/bin/cua approve --artifact artifacts/acme_core.lookup_savings_balance.cu_northgate/1.json

# the overlay itself declares this tenant's own scope
# ("allowed_route_patterns": ["/tenant-b/*"], overlays/.../cu_northgate.json)
# -- apply_overlay() merges it into the resolved artifact's app_profile, so
# nothing extra needs running here. Pointing this same resolved artifact's
# entry step at the BASE tenant's /members/search instead (not shown) is
# refused with policy_blocked, naming that declared pattern as the reason,
# even though /members/search is well within policy.yaml's own allowlist.

# same capability, same outputs shape, a surface it was never recorded on
.venv/bin/cua replay \
  --artifact artifacts/acme_core.lookup_savings_balance.cu_northgate/1.json \
  -p member_id=12345 -p branch=main
# -> status: success, typed Money output

# the business outcome carries over with NO override -- its detection text
# happens to match this tenant's build unchanged
.venv/bin/cua replay \
  --artifact artifacts/acme_core.lookup_savings_balance.cu_northgate/1.json \
  -p member_id=99999 -p branch=main
# -> status: business_outcome, code MEMBER_NOT_FOUND
```

## Tests

```bash
.venv/bin/pytest
.venv/bin/ruff check .
```

## Schema diagrams

The Pydantic contracts are rendered into version-controlled diagrams under
[`docs/schema/`](docs/schema/). Solid arrows show typed containment; the separate
`semantic-references.svg` uses dashed arrows for string IDs and templates. The
Capability artifact's cross-field validators enforce the relevant live relations;
the overlay links document the multi-tenant resolution `apply_overlay()` actually
performs (see the demo above), not a planned one.

```bash
brew install graphviz # macOS, once
.venv/bin/python scripts/render_schema_graph.py
.venv/bin/python scripts/render_schema_graph.py --check
```
