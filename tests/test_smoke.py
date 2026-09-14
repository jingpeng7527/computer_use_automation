"""Scaffold smoke tests. Real tests land alongside each module as it is built."""

from typer.testing import CliRunner

from cua.cli import app

runner = CliRunner()


def test_cli_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("serve-target", "discover", "harden", "replay", "ops", "catalog"):
        assert cmd in result.output


def test_ops_subcommands_exist() -> None:
    result = runner.invoke(app, ["ops", "--help"])
    assert result.exit_code == 0
    for cmd in ("claim", "release", "status"):
        assert cmd in result.output


def test_package_imports() -> None:
    import cua

    assert cua.__version__
