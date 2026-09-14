"""Single entrypoint for every moving part of the system.

    cua serve-target     # run the local mock legacy console (the "target app")
    cua operator         # run the mock operator console + escalation broker
    cua discover         # LLM-driven discovery run -> emits a capability artifact
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
) -> None:
    """Deterministically replay an artifact with input params. Never calls an LLM."""
    raise typer.Exit(code=_not_yet("replay"))


@app.command()
def catalog() -> None:
    """List saved capability artifacts with their input/output contracts."""
    raise typer.Exit(code=_not_yet("catalog"))


def _not_yet(cmd: str) -> int:
    typer.secho(f"`cua {cmd}` is not implemented yet (scaffold step).", fg=typer.colors.YELLOW)
    return 1


if __name__ == "__main__":
    app()
