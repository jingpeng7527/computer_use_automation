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
    raise typer.Exit(code=_not_yet("discover"))


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
