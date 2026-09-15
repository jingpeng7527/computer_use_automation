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
| `docs/schema/` | Generated Pydantic JSON Schema, DOT, and reviewable SVG contract diagrams |
| `config/policy.yaml` | Allowlist, risk tiers, redaction rules, execution bounds |
| `artifacts/` | Saved capability artifacts (one JSON file per version) |
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

# 4. replay: deterministic re-run of that artifact, no LLM, valid input
.venv/bin/cua replay \
  --artifact artifacts/acme_core.lookup_savings_balance/1.json \
  -p member_id=12345
# -> status: success, typed Money output

# 5. hardening: observe a real failure mode (no LLM) and bake it into the artifact
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
# -> artifacts/acme_core.lookup_savings_balance/2.json (status: "draft")
.venv/bin/cua approve --artifact artifacts/acme_core.lookup_savings_balance/2.json

# 6. replay against the hardened artifact with input that hits that outcome
.venv/bin/cua replay \
  --artifact artifacts/acme_core.lookup_savings_balance/2.json \
  -p member_id=99999
# -> status: business_outcome, code MEMBER_NOT_FOUND (not a crash)

# 7. escalation and handoff: force a failure, take control of the same live
#    browser session, fix it by hand, hand back, watch replay resume and finish
.venv/bin/cua replay \
  --artifact artifacts/acme_core.lookup_savings_balance/2.json \
  -p member_id=12345 \
  --fault "inject=500"
# stalls with: "if this run gets stuck: cua ops claim <run_id> ..."
# in a second terminal, once it's stuck:
.venv/bin/cua ops claim <run_id>
#   -> fix it by hand in the already-open browser window (e.g. reload without ?inject=500)
.venv/bin/cua ops release <run_id>
# the waiting replay process notices, re-checks its checkpoint, and resumes to SUCCESS
```

`--fault` is a dev/demo hook only: it appends a query string to the first navigate
step for that one run, to reproduce an error scenario without editing the saved
artifact. It never short-circuits the engine -- replay still observes the real page
and classifies whatever it actually sees; `evidence/README.md` explains how to check
that for yourself.

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
the overlay links document the planned multi-tenant stretch.

```bash
brew install graphviz # macOS, once
.venv/bin/python scripts/render_schema_graph.py
.venv/bin/python scripts/render_schema_graph.py --check
```
