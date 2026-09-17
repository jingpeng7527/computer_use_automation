"""Single entrypoint for every moving part of the system.

    cua serve-target     # run the local mock legacy console (the "target app")
    cua discover         # LLM-driven discovery run -> emits a capability artifact
    cua harden           # replay with bad input (no LLM), derive a runtime_match
    cua validate         # pre-approval proof for an artifact whose discovery needed a handoff
    cua approve          # promote a draft artifact to approved -- replay refuses drafts
    cua tag-output       # mark a declared output's sensitivity for replay-time redaction
    cua set-scope        # declare a capability's own base_url/route scope (narrows policy.yaml)
    cua replay           # deterministic replay of an artifact (no LLM)
    cua overlay apply    # resolve a base artifact + a tenant overlay -> a tenant artifact
    cua ops              # claim/release/status -- the human side of a handoff
    cua drift-report     # aggregate locator_layer_hit across runs into a per-step drift signal
    cua catalog          # list saved capability artifacts

Only `discover` ever calls an LLM.

There is no HTTP "operator console": the operator's real interface is the
already-open, headed browser window `cua replay` leaves on screen when it
gets stuck -- see REPORT.md sec 5 / 系统设计.md sec 1.6 for why a service
proxying that session was rejected. `cua ops` is the bookkeeping side only
(a SQLite row saying who's in control); the operator's actual "manual fix"
happens by hand, directly in that window, outside this CLI entirely.
"""

from __future__ import annotations

import typer
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
ops_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Claim/release control of a stuck run.")
app.add_typer(ops_app, name="ops")
overlay_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Resolve tenant overlays.")
app.add_typer(overlay_app, name="overlay")


@app.command("serve-target")
def serve_target(
    host: str = "127.0.0.1",
    port: int = 8800,
) -> None:
    """Run the local mock legacy credit-union servicing console."""
    import uvicorn

    uvicorn.run("cua.target_app.app:app", host=host, port=port, log_level="info")


@ops_app.command("claim")
def ops_claim(
    run_id: str,
    holder: str = typer.Option("operator", help="who's claiming it (a name, for the audit trail)."),
    lease_seconds: float = typer.Option(300, help="how long the claim is good for before it's reclaimable."),
) -> None:
    """Claim control of a PAUSED run. Fails if someone else already holds an unexpired lease."""
    from cua.escalation import ControlBroker

    broker = ControlBroker()
    if broker.claim(run_id, holder=holder, lease_seconds=lease_seconds):
        typer.secho(f"claimed {run_id!r} as {holder!r}.", fg=typer.colors.GREEN)
        typer.echo("the browser window cua replay left open is yours to operate directly.")
        typer.echo(f"when done: cua ops release {run_id} --holder {holder}")
    else:
        row = broker.get_state(run_id)
        typer.secho(
            f"could not claim {run_id!r}: current state is "
            f"{row.state if row else 'unknown'} (holder={row.holder if row else None!r}).",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)


@ops_app.command("release")
def ops_release(
    run_id: str,
    holder: str = typer.Option("operator"),
    note: str = typer.Option(
        None, help="What you actually did, for the human_action.json evidence record (optional but recommended)."
    ),
    resolution: str = typer.Option(
        None,
        help="Required for a discovery-phase intervention, forbidden for a replay-phase one: "
        "'cleared_obstacle' (hand back to the LLM with a fresh observation) or "
        "'workflow_advanced' (you did some/all of the task by hand -- aborts, no artifact).",
    ),
) -> None:
    """Hand control back. The waiting `cua replay`/`cua discover` process notices and resumes."""
    from cua.escalation import ControlBroker

    broker = ControlBroker()
    try:
        released = broker.release(run_id, holder=holder, note=note, resolution=resolution)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    if released:
        typer.secho(f"released {run_id!r}; the automation will resume.", fg=typer.colors.GREEN)
    else:
        typer.secho(f"could not release {run_id!r} as {holder!r} -- not your claim?", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@ops_app.command("status")
def ops_status(run_id: str) -> None:
    """Show the current control state of a run."""
    from cua.escalation import ControlBroker

    row = ControlBroker().get_state(run_id)
    if row is None:
        typer.echo(f"{run_id!r}: no record (never escalated, or a different db).")
        return
    typer.echo(f"{run_id!r}: state={row.state} holder={row.holder} reason={row.reason!r}")


@app.command()
def discover(
    goal: str = typer.Option(..., help="Natural-language goal for the target app."),
    target: str = typer.Option(..., help="Entry URL for the target app."),
    name: str = typer.Option(..., help="Capability name to save the artifact under."),
    max_steps: int = typer.Option(25, help="Stopping condition: max agent steps."),
    handoff: bool = typer.Option(
        True,
        help="on a no-progress stall, raise a discovery-phase intervention and wait (same live "
        "session) for 'cua ops claim/release --resolution ...' rather than failing immediately. "
        "Disable for a quick, non-interactive check of the raw stalled result.",
    ),
    handoff_timeout_s: float = typer.Option(120, help="give up waiting for an operator after this long."),
) -> None:
    """Run the LLM observe -> decide -> act loop until the goal is met, then emit an artifact."""
    import hashlib
    import json
    import time
    from datetime import UTC, datetime
    from pathlib import Path

    from cua.agent import (
        FallbackProvider,
        GeminiProvider,
        GroqProvider,
        compile_capability,
        extract_param_literals,
        run_discovery,
        template_literals,
    )
    from cua.safety import load_policy, mask, redact_text
    from cua.schema import AppProfile, ArtifactStore
    from cua.surface import WebAdapter

    run_id = f"discovery-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    evidence_dir = Path("evidence") / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    capability_id = f"acme_core.{name}"  # computed here, not just before compiling: a discovery-phase
    # intervention needs it as `requested_capability_id` -- the id this run is HEADED for, not a
    # claim that an artifact already exists (see escalation/intervention.py's docstring).

    policy = load_policy()
    try:
        secondary = GroqProvider()
    except RuntimeError:
        secondary = None  # no GROQ_API_KEY set -- Gemini alone, no fallback
    provider = FallbackProvider(primary=GeminiProvider(), secondary=secondary)
    model_name = provider.primary.model

    broker = None
    if handoff:
        from cua.escalation import ControlBroker

        broker = ControlBroker()
        typer.echo(
            f"(if this run stalls: cua ops claim {run_id} , fix it in the browser window, then "
            f"cua ops release {run_id} --resolution cleared_obstacle|workflow_advanced)"
        )

    adapter = WebAdapter(headless=False)
    started_at = time.time()
    try:
        transcript = run_discovery(
            goal,
            target,
            adapter,
            provider,
            policy,
            model_name=model_name,
            max_steps=max_steps,
            broker=broker,
            run_id=run_id,
            evidence_dir=str(evidence_dir),
            requested_capability_id=capability_id,
            escalation_max_wait_s=handoff_timeout_s,
        )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    finally:
        adapter.close()
    duration_s = time.time() - started_at

    # The model that actually reasoned, not just the one configured as
    # primary: if Gemini erred/rate-limited partway through and Groq picked
    # up the rest (FallbackProvider.last_used_model, captured per step),
    # evidence has to say so -- crediting the primary for a run it didn't
    # fully do would misreport the one thing this assignment can't fake.
    used_models: list[str] = []
    for s in transcript.steps:
        if s.provider_model and s.provider_model not in used_models:
            used_models.append(s.provider_model)
    actual_model = " -> ".join(used_models) if used_models else model_name
    transcript.model = actual_model  # compile_capability reads this into provenance.model

    # Redaction at the persistence boundary applies here too, not just to
    # replay's evidence: transcript.json / run_meta.json are the FIRST place
    # a discovery-time literal (a typed member id, a name echoed back on a
    # results page, an id embedded in a URL path) would otherwise get
    # written down. Two passes, matching safety/redaction.py's own two
    # mechanisms -- structural first (template out exactly the literals we
    # know are input values, the same way compile_capability templates the
    # goal), then a regex fallback for anything else with a recognisable
    # sensitive shape.
    param_literals = extract_param_literals(transcript.steps)

    def _sanitize(text: str | None) -> str | None:
        if text is None:
            return None
        return redact_text(template_literals(text, param_literals), policy)

    # NOT the same "declared outputs aren't redacted" rule replay follows
    # (engine.py's _redact_outputs) -- that rule presumes an OutputSpec
    # whose sensitivity a human has already reviewed. At discovery time no
    # such review has happened yet: the goal could just as easily have been
    # "read the account holder's name", and whatever the model actually
    # read lands in `transcript.outputs` with no OutputSpec, no sensitivity
    # tag, nothing to gate on. The regex fallback (_sanitize, below) only
    # catches a handful of fixed SHAPES -- SSNs, emails, long digit runs --
    # and a plain name like "Dolores Ibarra" matches none of them and
    # passed straight through when this only ran regex (verified: it did).
    # ctx.* values get exactly this same treatment in replay -- "treated as
    # sensitive by default rather than by tag ... the only kind whose
    # sensitivity is not declared in the artifact" -- because they too come
    # off a live screen with nothing to classify them yet. Discovery output
    # is the identical case, so it gets the identical answer: masked
    # unconditionally, not filtered by pattern.
    #
    # This has to be applied everywhere the same value can appear, not just
    # the top-level outputs dict -- verified it otherwise leaked twice more
    # from the exact same read: the `finish` tool call's own `outputs` arg
    # (a nested dict `_sanitize` never recursed into, since it only handled
    # top-level strings) and the "read" step's own observed node.text (the
    # literal page text a read step's whole purpose is to capture).
    outputs_evidence = {k: mask(v, "unclassified") for k, v in transcript.outputs.items()}

    def _sanitize_arg(v):
        if isinstance(v, str):
            return _sanitize(v)
        if isinstance(v, dict):
            return {k: mask(v2, "unclassified") if isinstance(v2, str) else v2 for k, v2 in v.items()}
        return v

    goal_evidence = _sanitize(goal)
    steps_evidence = [
        {
            "step_index": s.step_index,
            "tool_call": {
                "name": s.tool_call.name,
                "args": {k: _sanitize_arg(v) for k, v in s.tool_call.args.items()},
            },
            "node": (
                {
                    "node_id": s.node.node_id,
                    "role": s.node.role,
                    "name": _sanitize(s.node.name),
                    "text": (
                        mask(s.node.text, "unclassified")
                        if s.tool_call.name == "read" and s.node.text
                        else _sanitize(s.node.text)
                    ),
                }
                if s.node
                else None
            ),
            "location_path": _sanitize(s.location_path),
            "result": s.result,
            "detail": _sanitize(s.detail),
            "provider_model": s.provider_model,
        }
        for s in transcript.steps
    ]
    transcript_json = json.dumps(
        {"goal": goal_evidence, "target": target, "steps": steps_evidence, "outputs": outputs_evidence},
        indent=2,
    )
    (evidence_dir / "transcript.json").write_text(transcript_json)
    transcript_sha256 = hashlib.sha256(transcript_json.encode()).hexdigest()

    run_meta = {
        "run_id": run_id,
        "goal": goal_evidence,
        "target": target,
        "model": actual_model,
        "success": transcript.success,
        "reason": _sanitize(transcript.reason),
        "outputs": outputs_evidence,
        "step_count": len(transcript.steps),
        "duration_s": round(duration_s, 2),
    }
    (evidence_dir / "run_meta.json").write_text(json.dumps(run_meta, indent=2))

    if not transcript.success:
        typer.secho(f"discovery did not succeed: {transcript.reason}", fg=typer.colors.RED)
        typer.echo(f"evidence written to {evidence_dir}/")
        raise typer.Exit(code=1)

    capability_id = f"acme_core.{name}"
    capability = compile_capability(
        transcript,
        capability_id=capability_id,
        app_profile=AppProfile(product="acme_core", version="2024.1"),
        discovery_run_id=run_id,
        transcript_sha256=transcript_sha256,
    )
    (evidence_dir / "artifact_emitted.json").write_text(capability.model_dump_json(indent=2))

    store = ArtifactStore()
    saved_path = store.save(capability)

    typer.secho(f"discovery succeeded in {len(transcript.steps)} steps", fg=typer.colors.GREEN)
    typer.echo(f"artifact saved to {saved_path}")
    typer.echo(f"evidence written to {evidence_dir}/")


@app.command()
def replay(
    artifact: str = typer.Option(..., help="Path to a saved capability artifact (JSON)."),
    param: list[str] = typer.Option(None, "--param", "-p", help="key=value input param."),
    fault: str = typer.Option(
        None,
        help=(
            "dev/demo hook: appends '?<fault>' to the first navigate step's URL for this "
            "run only (e.g. --fault inject=500), to reproduce an error scenario without "
            "touching the saved artifact."
        ),
    ),
    handoff: bool = typer.Option(
        True,
        help="on a stuck step, raise an intervention and wait (same live session) for "
        "'cua ops claim/release' rather than returning immediately. Disable for a quick, "
        "non-interactive check of the raw failed/escalated result.",
    ),
    handoff_timeout_s: float = typer.Option(120, help="give up waiting for an operator after this long."),
) -> None:
    """Deterministically replay an artifact with input params. Never calls an LLM."""
    import json
    import time
    from datetime import UTC, datetime
    from pathlib import Path

    from cua.replay import replay as run_replay
    from cua.safety import load_policy
    from cua.schema import Capability, Navigate
    from cua.surface import WebAdapter

    params: dict[str, str] = {}
    for item in param or []:
        key, sep, value = item.partition("=")
        if not sep:
            typer.secho(f"--param must be key=value, got {item!r}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        params[key] = value

    capability = Capability.model_validate_json(Path(artifact).read_text())

    if fault:
        steps = list(capability.steps)
        if steps and isinstance(steps[0].action, Navigate):
            sep = "&" if "?" in steps[0].action.url_template else "?"
            faulted = steps[0].action.model_copy(
                update={"url_template": steps[0].action.url_template + sep + fault}
            )
            steps[0] = steps[0].model_copy(update={"action": faulted})
            capability = capability.model_copy(update={"steps": steps})

    run_id = f"replay-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    evidence_dir = Path("evidence") / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    policy = load_policy()
    adapter = WebAdapter(headless=False)
    broker = None
    if handoff:
        from cua.escalation import ControlBroker

        broker = ControlBroker()
        typer.echo(
            f"(if this run gets stuck: cua ops claim {run_id} , fix it in the browser "
            f"window, then cua ops release {run_id})"
        )
    started = time.time()
    try:
        result = run_replay(
            capability,
            params,
            adapter,
            policy,
            run_id=run_id,
            evidence_dir=str(evidence_dir),
            broker=broker,
            escalation_max_wait_s=handoff_timeout_s,
        )
    finally:
        adapter.close()

    (evidence_dir / "result.json").write_text(result.model_dump_json(indent=2))

    color = {
        "success": typer.colors.GREEN,
        "business_outcome": typer.colors.CYAN,
        "failed": typer.colors.RED,
        "escalated": typer.colors.YELLOW,
    }[result.status]
    typer.secho(f"status: {result.status}  ({time.time() - started:.2f}s)", fg=color)
    if result.status == "success":
        # pydantic's own serialization, not stdlib json.dumps -- an output
        # can be a Money object now, which json.dumps doesn't know how to
        # encode on its own.
        dumped = result.model_dump(mode="json")["outputs"]
        typer.echo(f"outputs: {json.dumps(dumped, indent=2)}")
    elif result.status == "business_outcome":
        dumped = result.model_dump(mode="json")["outcome"]["outputs"]
        typer.echo(f"code: {result.outcome.code}  outputs: {json.dumps(dumped)}")
    elif result.status == "failed":
        typer.echo(
            f"step {result.failure.step_id!r} ({result.failure.kind}): "
            f"expected {result.failure.expected!r}, observed {result.failure.observed!r}"
        )
    elif result.status == "escalated":
        typer.echo(f"reason: {result.escalation.reason}")
    typer.echo(f"evidence written to {evidence_dir}/")

    raise typer.Exit(code=0 if result.status in ("success", "business_outcome") else 1)


@app.command()
def harden(
    artifact: str = typer.Option(..., help="Path to a saved capability artifact (JSON)."),
    param: list[str] = typer.Option(
        ..., "--param", "-p", help="key=value BAD input param, deliberately invalid."
    ),
    outcome_code: str = typer.Option(
        None,
        help="Business-outcome code to assign if the divergence classifies as one "
        "(e.g. MEMBER_NOT_FOUND). Required only when it does.",
    ),
    detect_text: str = typer.Option(
        None,
        help="Exact text on the divergent page that identifies this outcome (e.g. "
        "'No member records match'). Required to save; omit on the first run to see "
        "the candidates and pick one.",
    ),
) -> None:
    """Re-run a capability's steps (no LLM) with deliberately bad input, classify
    what actually happens on screen, and add the resulting runtime_match to the
    artifact. Never guesses a failure mode -- only records one actually observed."""
    from datetime import UTC, datetime
    from pathlib import Path

    from cua.agent import build_runtime_match, run_hardening_pass
    from cua.safety import load_policy
    from cua.schema import ArtifactStore, Capability
    from cua.surface import WebAdapter

    params: dict[str, str] = {}
    for item in param:
        key, sep, value = item.partition("=")
        if not sep:
            typer.secho(f"--param must be key=value, got {item!r}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        params[key] = value

    capability = Capability.model_validate_json(Path(artifact).read_text())
    run_id = f"harden-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"

    policy = load_policy()
    adapter = WebAdapter(headless=False)
    try:
        category, candidate_texts, result = run_hardening_pass(
            capability, params, adapter, policy, run_id=run_id
        )
    finally:
        adapter.close()

    typer.secho(
        f"diverged at step {result.failure.step_id!r}: classified as {category}",
        fg=typer.colors.CYAN,
    )
    for text in candidate_texts:
        typer.echo(f"  page text: {text!r}")

    if not detect_text:
        typer.secho(
            "pass --detect-text '<one of the lines above>' to save this runtime_match.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)
    if not any(detect_text in c for c in candidate_texts):
        # substring, not exact match -- the curator generalizes the message
        # (e.g. drop the specific id that was searched) so it matches on
        # replay regardless of which invalid input was supplied.
        typer.secho(
            f"--detect-text {detect_text!r} isn't a substring of anything actually on "
            f"the page; not saving.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    if category == "business_outcome" and not outcome_code:
        typer.secho(
            "a business_outcome needs --outcome-code to name it; not saving.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    match = build_runtime_match(
        after_step=result.failure.step_id,
        category=category,
        detect_text=detect_text,
        outcome_code=outcome_code if category == "business_outcome" else None,
    )

    possible_outcomes = list(capability.possible_outcomes)
    if match.result_code and match.result_code not in possible_outcomes:
        possible_outcomes.append(match.result_code)

    # model_copy() doesn't re-validate; round-trip through model_validate so
    # an inconsistent update (e.g. a code no rule can produce) is caught now.
    updated = Capability.model_validate(
        {
            **capability.model_dump(mode="json"),
            "version": capability.version + 1,
            "status": "draft",  # a new runtime_match is new, unreviewed behaviour --
            # inheriting "approved" from the base would let it go straight to
            # unattended replay with no review of what hardening just added,
            # defeating the point of the approval gate below.
            "runtime_matches": [*capability.runtime_matches, match.model_dump(mode="json")],
            "possible_outcomes": possible_outcomes,
        }
    )

    saved_path = ArtifactStore().save(updated)
    typer.secho(
        f"updated artifact saved to {saved_path} (status: draft -- run `cua approve` before replay)",
        fg=typer.colors.YELLOW,
    )


@app.command()
def validate(
    artifact: str = typer.Option(..., help="Path to a draft artifact that recorded a discovery handoff."),
    param: list[str] = typer.Option(..., "--param", "-p", help="key=value input param for the validation run."),
) -> None:
    """Pre-approval validation for an artifact whose discovery run needed a
    human handoff (Provenance.discovery_handoffs > 0): replays it from a
    FRESH browser session, with `status` flipped to "approved" only in
    memory (never written to the file on disk -- see this command's own
    module docstring section, agent/loop.py's discovery-handoff design, and
    REPORT.md sec 5/7), and with escalation hard-disabled. Getting stuck
    here is a validation FAILURE, not a second chance to have a human help
    -- the entire point is proving the recorded LLM steps hold up
    unattended, on their own, with nobody watching.

    Writes `evidence/<run_id>/validation.json`, which `cua approve
    --validation-run <run_id>` checks before promoting an artifact that
    needed a handoff: this artifact's own content hash (excluding
    `status`), so a subsequent edit invalidates the proof; capability_id
    and version; and confirmation this validation run itself never
    escalated (handoff_count is always exactly 0 -- there is no code path
    in this command that could set it otherwise)."""
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from cua.replay import replay as run_replay
    from cua.safety import load_policy
    from cua.schema import Capability, approval_snapshot_sha256
    from cua.surface import WebAdapter

    params: dict[str, str] = {}
    for item in param:
        key, sep, value = item.partition("=")
        if not sep:
            typer.secho(f"--param must be key=value, got {item!r}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        params[key] = value

    capability = Capability.model_validate_json(Path(artifact).read_text())
    if capability.status != "draft":
        typer.secho(
            f"{artifact} is {capability.status!r}, not 'draft' -- cua validate is a pre-approval "
            f"check, nothing left to validate before promoting an already-approved artifact.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    if capability.provenance.discovery_handoffs == 0:
        typer.secho(
            f"{artifact} recorded no discovery handoff -- cua validate exists for the artifacts "
            f"that did; run `cua approve` on this one directly.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    # In-memory only: replay() itself is never modified or bypassed to
    # accept this. A real `cua replay` against the SAME file on disk still
    # refuses it -- draft is draft until a human runs `cua approve` for
    # real. Round-tripped through model_validate (not a bare model_copy)
    # so an otherwise-invalid artifact can't slip through here either.
    in_memory_approved = Capability.model_validate(
        {**capability.model_dump(mode="json"), "status": "approved"}
    )

    run_id = f"validate-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    evidence_dir = Path("evidence") / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    policy = load_policy()
    adapter = WebAdapter(headless=False)
    try:
        result = run_replay(
            in_memory_approved, params, adapter, policy, run_id=run_id, evidence_dir=str(evidence_dir)
            # no `broker` -- escalation is hard-disabled, not merely defaulted off, matching this
            # command's own scope: getting stuck here is what a validation is FOR catching.
        )
    finally:
        adapter.close()

    record = {
        "run_id": run_id,
        "mode": "preapproval_validation",
        "capability_id": capability.capability_id,
        "version": capability.version,
        "approval_snapshot_sha256": approval_snapshot_sha256(capability),
        "status": result.status,
        "handoff_count": 0,
        "fresh_session": True,
    }
    (evidence_dir / "validation.json").write_text(json.dumps(record, indent=2))
    (evidence_dir / "result.json").write_text(result.model_dump_json(indent=2))

    if result.status == "success":
        typer.secho(f"validation PASSED: {artifact} replays clean from a fresh session.", fg=typer.colors.GREEN)
        typer.echo(f"cua approve --artifact {artifact} --validation-run {run_id}")
    else:
        typer.secho(
            f"validation FAILED: status={result.status!r} -- {artifact} is not ready for approval.",
            fg=typer.colors.RED,
        )
        typer.echo(f"evidence written to {evidence_dir}/")
        raise typer.Exit(code=1)


@app.command()
def approve(
    artifact: str = typer.Option(..., help="Path to a saved capability artifact (JSON)."),
    validation_run: str = typer.Option(
        None,
        "--validation-run",
        help="Required when the artifact's provenance shows a discovery handoff: the `cua "
        "validate` run_id whose evidence proves a clean, unattended replay of THIS exact content.",
    ),
) -> None:
    """Promote a draft artifact to approved -- the human review step
    unattended replay now requires (REPORT.md sec 7). A real workflow would
    gate this on a signed review; this is the CLI-only version the brief's
    scope calls for. Saves in place at the same version, so the resulting
    git diff is exactly the one-line status flip a reviewer approved.

    An artifact whose discovery run needed a human handoff cannot be
    approved directly, no matter what --validation-run is passed as: its
    LLM-recorded steps ran partly on a page a human already reached into,
    so nothing here has yet proven they hold up completely unattended.
    `cua validate` is the one way to produce that proof; this command only
    ever checks evidence already on disk, never runs a browser itself."""
    import json
    from pathlib import Path

    from cua.schema import ArtifactStore, Capability, approval_snapshot_sha256

    capability = Capability.model_validate_json(Path(artifact).read_text())
    if capability.status == "approved":
        typer.echo(f"{artifact} is already approved.")
        return

    if capability.provenance.discovery_handoffs > 0:
        if not validation_run:
            typer.secho(
                f"{artifact} recorded {capability.provenance.discovery_handoffs} discovery "
                f"handoff(s) -- run `cua validate --artifact {artifact} -p ...` first, then pass "
                f"--validation-run <its run_id> here.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)
        validation_path = Path("evidence") / validation_run / "validation.json"
        if not validation_path.exists():
            typer.secho(f"no validation evidence found at {validation_path}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        record = json.loads(validation_path.read_text())
        problems = []
        if record.get("mode") != "preapproval_validation":
            problems.append(f"mode={record.get('mode')!r}, expected 'preapproval_validation'")
        if record.get("status") != "success":
            problems.append(f"status={record.get('status')!r}, expected 'success'")
        if record.get("handoff_count") != 0:
            problems.append(f"handoff_count={record.get('handoff_count')!r} -- validation itself was not clean")
        if record.get("capability_id") != capability.capability_id:
            problems.append("capability_id does not match this artifact")
        if record.get("version") != capability.version:
            problems.append("version does not match this artifact")
        current_hash = approval_snapshot_sha256(capability)
        if record.get("approval_snapshot_sha256") != current_hash:
            problems.append(
                f"{artifact} has changed since {validation_run!r} validated it -- re-run cua validate"
            )
        if problems:
            typer.secho(f"validation run {validation_run!r} does not prove this artifact is safe to approve:", fg=typer.colors.RED)
            for p in problems:
                typer.secho(f"  {p}", fg=typer.colors.RED)
            raise typer.Exit(code=1)

    # model_copy() doesn't re-validate; round-trip through model_validate so
    # an otherwise-invalid artifact can't slip through on the way to approval.
    approved = Capability.model_validate(
        {**capability.model_dump(mode="json"), "status": "approved"}
    )
    saved_path = ArtifactStore().save(approved)
    typer.secho(f"approved: {saved_path}", fg=typer.colors.GREEN)


@app.command("tag-output")
def tag_output(
    artifact: str = typer.Option(..., help="Path to a saved capability artifact (JSON)."),
    name: str = typer.Option(..., help="Output name to tag (must match a declared OutputSpec)."),
    sensitivity: str = typer.Option(..., help="none | pii | sensitive"),
) -> None:
    """Mark a declared output's sensitivity -- the human review step that
    makes replay's own output redaction (engine.py's _redact_outputs)
    actually mask something. Nothing sets this automatically: discovery
    only ever emits sensitivity="none" on every output it captures, so
    without this command a capability built to read, say, an account
    holder's name would return it unmasked forever. Resets status to
    draft -- this changes what the capability's result contract reveals to
    a caller, which is exactly the kind of change Section 7's approval
    gate exists to have a human look at again."""
    from pathlib import Path

    from pydantic import ValidationError

    from cua.schema import ArtifactStore, Capability

    capability = Capability.model_validate_json(Path(artifact).read_text())
    names = {o.name for o in capability.outputs}
    if name not in names:
        typer.secho(
            f"{name!r} is not a declared output of this capability (has: {sorted(names)})",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    outputs = [
        {**o.model_dump(mode="json"), "sensitivity": sensitivity} if o.name == name else o.model_dump(mode="json")
        for o in capability.outputs
    ]
    try:
        updated = Capability.model_validate(
            {**capability.model_dump(mode="json"), "outputs": outputs, "status": "draft"}
        )
    except ValidationError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    saved_path = ArtifactStore().save(updated)
    typer.secho(
        f"tagged {name!r} as sensitivity={sensitivity!r}; saved to {saved_path} "
        f"(status: draft -- run `cua approve` before replay)",
        fg=typer.colors.YELLOW,
    )


@overlay_app.command("apply")
def overlay_apply(
    base: str = typer.Option(..., help="Path to the base capability artifact (JSON)."),
    overlay: str = typer.Option(..., help="Path to the tenant overlay (JSON)."),
) -> None:
    """Resolve a base artifact + a tenant overlay into a concrete, tenant-
    specific capability -- REPORT.md sec 4's multi-tenant reuse mechanism.
    Never touches a browser: this is pure schema resolution, and every
    existing Capability invariant (referential integrity, runtime_match
    consistency, ...) is re-checked against the RESOLVED steps before
    anything is saved. Always saves as status="draft" -- a resolved
    overlay is new composition a human hasn't reviewed yet; approve it
    like any other artifact before replaying it."""
    from pathlib import Path

    from pydantic import ValidationError

    from cua.overlay import apply_overlay
    from cua.schema import ArtifactStore, Capability, Overlay

    base_capability = Capability.model_validate_json(Path(base).read_text())
    overlay_doc = Overlay.model_validate_json(Path(overlay).read_text())
    try:
        resolved = apply_overlay(base_capability, overlay_doc)
    except (ValueError, ValidationError) as exc:
        typer.secho(f"overlay rejected: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    saved_path = ArtifactStore().save(resolved)
    typer.secho(
        f"resolved {overlay_doc.tenant_id!r} -> {saved_path} "
        f"(status: draft -- run `cua approve` before replay)",
        fg=typer.colors.GREEN,
    )


def _navigate_targets_outside_scope(capability, app_profile_data: dict) -> list[str]:
    """Best-effort pre-flight for `cua set-scope`: checks every step whose
    action is a Navigate with a LITERAL (non-templated) url_template
    against the scope about to be saved, using the exact same narrowing
    check replay enforces (safety.check_app_profile_scope) so this can
    never drift from what would actually happen at replay time.

    Deliberately partial: a step that reaches a new page by clicking a
    link (this project's own capability does exactly that for its detail
    page) has no literal URL recorded in the artifact at all -- its
    destination is only known by actually running the flow, which this
    command does not do. This catches the entry point and any other
    literal Navigate being scoped out from under itself; it does not
    guarantee every step in the capability still works."""
    from urllib.parse import urlparse

    from cua.safety import check_app_profile_scope
    from cua.schema import AppProfile, Navigate

    profile = AppProfile.model_validate(app_profile_data)
    problems = []
    for step in capability.steps:
        if not isinstance(step.action, Navigate):
            continue
        url = step.action.url_template
        if "{{" in url:
            continue  # not a literal -- can't be statically resolved here
        path = urlparse(url).path or "/"
        decision = check_app_profile_scope(url, path, profile)
        if not decision.allowed:
            problems.append(f"step {step.id!r} navigates to {url!r}: {decision.reason}")
    return problems


@app.command("set-scope")
def set_scope(
    artifact: str = typer.Option(..., help="Path to a saved capability artifact (JSON)."),
    base_url: str = typer.Option(None, help="Restrict this capability to destinations starting with this URL."),
    allowed_route_pattern: list[str] = typer.Option(
        None, "--allowed-route-pattern", help="Glob route pattern this capability may touch (repeatable)."
    ),
    force: bool = typer.Option(
        False, help="Save even if this scope would already block one of this capability's own literal Navigate steps."
    ),
) -> None:
    """Declare a capability's own expected scope -- an ADDITIONAL narrowing
    of policy.yaml's allowlist, never a wider grant (safety/allowlist.py's
    check_allowed takes the intersection of the two). Lets a reviewer see,
    from the artifact alone, exactly which routes it's meant to touch,
    without cross-referencing policy.yaml. Resets status to draft: this
    changes the enforced boundary the capability runs inside, which is
    exactly the kind of change the approval gate exists to have a human
    look at again.

    Refuses (fail-loud, not just fail-safe) to save a scope that would
    already block one of this capability's own literal Navigate targets --
    without this check, that mistake would only surface later, mid-replay,
    as a `policy_blocked` failure on whichever step hit it first. Pass
    --force to save anyway. This check is necessarily partial: a
    click-driven destination has no literal URL in the artifact to check
    (see _navigate_targets_outside_scope's docstring)."""
    from pathlib import Path

    from pydantic import ValidationError

    from cua.schema import ArtifactStore, Capability

    if base_url is None and not allowed_route_pattern:
        typer.secho("pass --base-url and/or --allowed-route-pattern (at least one).", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    capability = Capability.model_validate_json(Path(artifact).read_text())
    app_profile = capability.app_profile.model_dump(mode="json")
    if base_url is not None:
        app_profile["base_url"] = base_url
    if allowed_route_pattern:
        app_profile["allowed_route_patterns"] = allowed_route_pattern

    problems = _navigate_targets_outside_scope(capability, app_profile)
    if problems:
        color = typer.colors.YELLOW if force else typer.colors.RED
        typer.secho(
            "this scope already excludes one of this capability's own literal Navigate steps:", fg=color
        )
        for problem in problems:
            typer.secho(f"  {problem}", fg=color)
        typer.secho(
            "(only literal Navigate targets are checkable here -- a click-driven destination has no "
            "URL recorded in the artifact to check against)",
            fg=color,
        )
        if not force:
            typer.secho("refusing to save. Pass --force to save anyway.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        typer.secho("--force given: saving anyway.", fg=typer.colors.YELLOW)

    try:
        updated = Capability.model_validate(
            {**capability.model_dump(mode="json"), "app_profile": app_profile, "status": "draft"}
        )
    except ValidationError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    saved_path = ArtifactStore().save(updated)
    typer.secho(
        f"scope set: base_url={updated.app_profile.base_url!r} "
        f"allowed_route_patterns={updated.app_profile.allowed_route_patterns!r}; saved to {saved_path} "
        f"(status: draft -- run `cua approve` before replay)",
        fg=typer.colors.YELLOW,
    )


@app.command("drift-report")
def drift_report(
    evidence_dir: str = typer.Option("evidence", help="Directory containing one subdirectory per run."),
    persistence_window: int = typer.Option(
        3, help="how many of the most recent runs must ALL be deeper than baseline to count as 'drifting'."
    ),
) -> None:
    """Aggregate every run's locator_layer_hit (already recorded per step by
    replay) into a per-(capability, step) drift signal: a step landing
    steadily deeper than where it used to resolve means the app changed
    underneath the artifact and it's due for a new version or a tenant
    override (REPORT.md sec 7)."""
    from cua.observability import aggregate_layer_drift, render_drift_report

    reports = aggregate_layer_drift(evidence_dir, persistence_window=persistence_window)
    typer.echo(render_drift_report(reports))
    if any(r.status == "drifting" for r in reports):
        raise typer.Exit(code=1)


@app.command()
def catalog() -> None:
    """List saved capability artifacts with their input/output contracts."""
    raise typer.Exit(code=_not_yet("catalog"))


def _not_yet(cmd: str) -> int:
    typer.secho(f"`cua {cmd}` is not implemented yet (scaffold step).", fg=typer.colors.YELLOW)
    return 1


if __name__ == "__main__":
    app()
