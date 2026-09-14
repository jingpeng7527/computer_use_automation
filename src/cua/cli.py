"""Single entrypoint for every moving part of the system.

    cua serve-target     # run the local mock legacy console (the "target app")
    cua operator         # run the mock operator console + escalation broker
    cua discover         # LLM-driven discovery run -> emits a capability artifact
    cua harden           # replay with bad input (no LLM), derive a runtime_match
    cua replay           # deterministic replay of an artifact (no LLM)
    cua catalog          # list saved capability artifacts

Only `discover` ever calls an LLM.
"""

from __future__ import annotations

import typer
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)


@app.command("serve-target")
def serve_target(
    host: str = "127.0.0.1",
    port: int = 8800,
) -> None:
    """Run the local mock legacy credit-union servicing console."""
    import uvicorn

    uvicorn.run("cua.target_app.app:app", host=host, port=port, log_level="info")


@app.command("operator")
def operator(
    host: str = "127.0.0.1",
    port: int = 8850,
) -> None:
    """Run the mock operator console + human-in-the-loop escalation broker."""
    import uvicorn

    uvicorn.run("cua.escalation.operator_app:app", host=host, port=port, log_level="info")


@app.command()
def discover(
    goal: str = typer.Option(..., help="Natural-language goal for the target app."),
    target: str = typer.Option(..., help="Entry URL for the target app."),
    name: str = typer.Option(..., help="Capability name to save the artifact under."),
    max_steps: int = typer.Option(25, help="Stopping condition: max agent steps."),
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
        run_discovery,
    )
    from cua.safety import load_policy
    from cua.schema import AppProfile, ArtifactStore
    from cua.surface import WebAdapter

    run_id = f"discovery-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    evidence_dir = Path("evidence") / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    policy = load_policy()
    try:
        secondary = GroqProvider()
    except RuntimeError:
        secondary = None  # no GROQ_API_KEY set -- Gemini alone, no fallback
    provider = FallbackProvider(primary=GeminiProvider(), secondary=secondary)
    model_name = provider.primary.model

    adapter = WebAdapter(headless=False)
    started_at = time.time()
    try:
        transcript = run_discovery(
            goal, target, adapter, provider, policy, model_name=model_name, max_steps=max_steps
        )
    finally:
        adapter.close()
    duration_s = time.time() - started_at

    steps_evidence = [
        {
            "step_index": s.step_index,
            "tool_call": {"name": s.tool_call.name, "args": s.tool_call.args},
            "node": (
                {"node_id": s.node.node_id, "role": s.node.role, "name": s.node.name, "text": s.node.text}
                if s.node
                else None
            ),
            "location_path": s.location_path,
            "result": s.result,
            "detail": s.detail,
        }
        for s in transcript.steps
    ]
    transcript_json = json.dumps(
        {"goal": goal, "target": target, "steps": steps_evidence, "outputs": transcript.outputs},
        indent=2,
    )
    (evidence_dir / "transcript.json").write_text(transcript_json)
    transcript_sha256 = hashlib.sha256(transcript_json.encode()).hexdigest()

    run_meta = {
        "run_id": run_id,
        "goal": goal,
        "target": target,
        "model": model_name,
        "success": transcript.success,
        "reason": transcript.reason,
        "outputs": transcript.outputs,
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
    started = time.time()
    try:
        result = run_replay(
            capability, params, adapter, policy, run_id=run_id, evidence_dir=str(evidence_dir)
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
        typer.echo(f"outputs: {json.dumps(result.outputs, indent=2)}")
    elif result.status == "business_outcome":
        typer.echo(f"code: {result.outcome.code}  outputs: {json.dumps(result.outcome.outputs)}")
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
            "runtime_matches": [*capability.runtime_matches, match.model_dump(mode="json")],
            "possible_outcomes": possible_outcomes,
        }
    )

    saved_path = ArtifactStore().save(updated)
    typer.secho(f"updated artifact saved to {saved_path}", fg=typer.colors.GREEN)


@app.command()
def catalog() -> None:
    """List saved capability artifacts with their input/output contracts."""
    raise typer.Exit(code=_not_yet("catalog"))


def _not_yet(cmd: str) -> int:
    typer.secho(f"`cua {cmd}` is not implemented yet (scaffold step).", fg=typer.colors.YELLOW)
    return 1


if __name__ == "__main__":
    app()
