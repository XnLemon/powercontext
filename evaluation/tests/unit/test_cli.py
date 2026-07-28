from typer.testing import CliRunner

from powercontext_eval.cli import app


def test_cli_help_describes_the_evaluation_runner() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "PowerContext evaluation runner" in result.output
    assert not isinstance(result.exception, RuntimeError)
