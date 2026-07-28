"""Command-line entry point for the evaluation runner."""

import json

import typer

from powercontext_eval.powercontext_sut import run_codex_contract_smoke

app = typer.Typer(no_args_is_help=True, help="PowerContext evaluation runner.")


@app.callback()
def root() -> None:
    """Run reproducible PowerContext evaluations."""


@app.command("codex-contract-smoke")
def codex_contract_smoke(
    run_root: str = typer.Option(...),
    task_image: str = typer.Option(...),
    codex_bin: str = typer.Option(...),
    uv_bin: str = typer.Option(...),
    powercontext_source: str = typer.Option(...),
    powercontext_sha: str = typer.Option(...),
    auth_json: str = typer.Option(...),
    proxy_url: str = typer.Option(...),
    prompt: str = typer.Option("Reply with exactly OK."),
) -> None:
    """Run the disposable Codex OFF/ON contract smoke."""

    outcome = run_codex_contract_smoke(
        run_root=run_root,
        task_image=task_image,
        codex_bin=codex_bin,
        uv_bin=uv_bin,
        powercontext_source=powercontext_source,
        powercontext_sha=powercontext_sha,
        auth_json=auth_json,
        proxy_url=proxy_url,
        prompt=prompt,
    )
    typer.echo(json.dumps(outcome, ensure_ascii=False, sort_keys=True))


def main() -> None:
    """Run the evaluation command-line application."""

    app()


if __name__ == "__main__":
    main()
