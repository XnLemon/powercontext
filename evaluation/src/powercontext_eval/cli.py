"""Command-line entry point for the evaluation runner."""

import json

import typer

from powercontext_eval.powercontext_sut import run_codex_contract_smoke
from powercontext_eval.runner import MinimalRunConfig, run_minimal_swebench_pro

app = typer.Typer(no_args_is_help=True, help="PowerContext evaluation runner.")
swebench_pro_app = typer.Typer(no_args_is_help=True, help="Pinned SWE-bench Pro evaluation.")
app.add_typer(swebench_pro_app, name="swebench-pro")


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


@swebench_pro_app.command("run")
def swebench_pro_run(
    root_path: str = typer.Option("/data/powercontext-eval", "--root"),
    powercontext_source: str = typer.Option("/data/powercontext-eval/deploy/powercontext"),
    powercontext_ref: str = typer.Option("latest"),
    harness_root: str = typer.Option("/data/powercontext-eval/cache/swebench-pro.git"),
    harness_python: str = typer.Option("/data/powercontext-eval/venvs/swebench-pro-ca10a60/bin/python"),
    raw_sample_path: str = typer.Option("/data/powercontext-eval/cache/dataset/instance.jsonl"),
    codex_bin: str = typer.Option("/data/powercontext-eval/bin/codex"),
    uv_bin: str = typer.Option("/data/powercontext-eval/bin/uv"),
    auth_json: str = typer.Option("/data/powercontext-eval/codex-home/auth.json"),
    proxy_url: str = typer.Option("http://127.0.0.1:7890"),
    run_id: str | None = typer.Option(None),
) -> None:
    """Run Gold, PowerContext OFF/ON, official grading, and report generation."""

    from pathlib import Path

    result = run_minimal_swebench_pro(
        MinimalRunConfig(
            root=Path(root_path),
            powercontext_source=Path(powercontext_source),
            powercontext_ref=powercontext_ref,
            harness_root=Path(harness_root),
            harness_python=Path(harness_python),
            raw_sample_path=Path(raw_sample_path),
            codex_binary=Path(codex_bin),
            uv_binary=Path(uv_bin),
            auth_json=Path(auth_json),
            proxy_url=proxy_url,
            run_id=run_id,
        )
    )
    typer.echo(
        json.dumps(
            {
                "run_id": result.run_id,
                "report": str(result.report_path),
                "off_resolved": result.off_resolved,
                "on_resolved": result.on_resolved,
            },
            sort_keys=True,
        )
    )


def main() -> None:
    """Run the evaluation command-line application."""

    app()


if __name__ == "__main__":
    main()
