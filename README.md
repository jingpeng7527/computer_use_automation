# computer-use automation

An LLM discovers how to complete a goal inside a legacy back-office UI once. The run
is recorded as a typed, versioned **capability artifact**. From then on, a
**deterministic replay engine** re-runs that flow with no LLM in the decision loop,
returns typed outputs, and separates expected business outcomes from recoverable
conditions and hard failures. When it cannot safely proceed, it escalates to a human
who takes control of the same live session and hands it back.

> Status: work in progress. See `REPORT.md` for design rationale.

## Layout

| Path | What |
| --- | --- |
| `src/cua/target_app/` | Local mock legacy credit-union servicing console (the target surface) |
| `src/cua/surface/` | Surface abstraction: perceive/act seam (Playwright web impl) |
| `src/cua/schema/` | Capability artifact schema + result contract (Pydantic) |
| `src/cua/agent/` | LLM discovery loop (observe -> decide -> act) |
| `src/cua/replay/` | Deterministic replay engine + error taxonomy |
| `src/cua/safety/` | Allowlist enforcement + redaction |
| `src/cua/escalation/` | Stuck detection, control-transfer broker, mock operator console |
| `config/policy.yaml` | Allowlist, risky-action policy, redaction rules |
| `artifacts/` | Saved capability artifacts |
| `evidence/` | Committed evidence from discovery + replay runs |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium
cp .env.example .env   # set GEMINI_API_KEY for the discovery run (free tier)
```

The discovery run needs a Gemini API key (free tier: https://aistudio.google.com/apikey).
**Replay needs no key and no LLM.**

## Demo path

```bash
# 1. start the target app
.venv/bin/cua serve-target

# 2. (later) discovery run -> writes artifacts/<name>.json + evidence/
.venv/bin/cua discover --goal "..." --target http://127.0.0.1:8800 --name lookup_savings_balance

# 3. (later) deterministic replay with input params
.venv/bin/cua replay --artifact artifacts/lookup_savings_balance.json -p member_id=10001
```
