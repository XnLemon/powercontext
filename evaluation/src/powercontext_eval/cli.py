"""Command-line entry point for the evaluation runner."""

import typer

app = typer.Typer(no_args_is_help=True)


def main() -> None:
    """Run the evaluation command-line application."""

    app()
