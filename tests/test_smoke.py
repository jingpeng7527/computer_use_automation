"""Scaffold smoke tests. Real tests land alongside each module as it is built."""

from typer.testing import CliRunner

from cua.cli import app

runner = CliRunner()


def test_cli_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("serve-target", "operator", "discover", "replay", "catalog"):
        assert cmd in result.output


def test_package_imports() -> None:
    import cua

    assert cua.__version__
