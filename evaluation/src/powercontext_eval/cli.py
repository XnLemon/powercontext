"""Command-line entry point for the evaluation runner."""

import typer

app = typer.Typer(no_args_is_help=True, help="PowerContext evaluation runner.")


@app.callback()
def root() -> None:
    """Run reproducible PowerContext evaluations."""


def main() -> None:
    """Run the evaluation command-line application."""

    app()
