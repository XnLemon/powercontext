"""Command-line entry point for the evaluation runner."""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Annotated, Any, Protocol

import typer
from pydantic import ValidationError

from powercontext_eval.benchmarks.swebench_pro.catalog import SweBenchProCatalog
from powercontext_eval.powercontext_sut import run_codex_contract_smoke
from powercontext_eval.runner import RunConfig, run_swebench_pro_instance

if TYPE_CHECKING:
    from powercontext_eval.web.config import WebConfig

app = typer.Typer(no_args_is_help=True, help="PowerContext evaluation runner.")
swebench_pro_app = typer.Typer(no_args_is_help=True, help="Pinned SWE-bench Pro evaluation.")
app.add_typer(swebench_pro_app, name="swebench-pro")


@app.callback()
def root() -> None:
    """Run reproducible PowerContext evaluations."""


class _Stoppable(Protocol):
    def stop(self) -> None: ...


def _request_worker_stop(worker: _Stoppable, _signum: int, _frame: FrameType | None) -> None:
    """Request that a worker exit after its current task finishes."""
    worker.stop()


def _web_config(root_path: Path | None) -> WebConfig:
    from powercontext_eval.web.config import WebConfig

    try:
        environ = dict(os.environ)
        if root_path is not None:
            environ["POWERCONTEXT_EVAL_ROOT"] = os.fspath(root_path)
        return WebConfig.from_environment(environ)
    except (KeyError, TypeError, ValueError, ValidationError):
        raise typer.BadParameter("Invalid evaluation configuration.", param_hint="--root") from None


@contextmanager
def _worker_signal_handlers(worker: _Stoppable) -> Iterator[None]:
    previous: dict[signal.Signals, Any] = {}
    handler: Callable[[int, FrameType | None], None] = lambda signum, frame: _request_worker_stop(worker, signum, frame)
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        yield
    finally:
        for signum, prior in previous.items():
            signal.signal(signum, prior)


@app.command("web")
def web(root_path: Annotated[Path | None, typer.Option("--root")] = None) -> None:
    """Serve the evaluation console API and frontend."""
    import uvicorn

    from powercontext_eval.web.api import create_app

    config = _web_config(root_path)
    uvicorn.run(create_app(config), host=config.host, port=config.port)


@app.command("worker")
def worker(root_path: Annotated[Path | None, typer.Option("--root")] = None) -> None:
    """Run queued evaluations serially until shutdown is requested."""
    from powercontext_eval.web.store import TaskStore
    from powercontext_eval.web.worker import EvaluationWorker

    config = _web_config(root_path)
    store = TaskStore(config.database_path, lease_duration=timedelta(seconds=config.lease_seconds))
    store.initialize()
    service = EvaluationWorker(config, store)
    with _worker_signal_handlers(service):
        service.run_forever()


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
    dataset_path: str = typer.Option(
        "/data/powercontext-eval/cache/swebench-pro.git/helper_code/sweap_eval_full_v2.jsonl"
    ),
    instance_id: str = typer.Option(...),
    codex_bin: str = typer.Option("/data/powercontext-eval/bin/codex"),
    uv_bin: str = typer.Option("/data/powercontext-eval/bin/uv"),
    auth_json: str = typer.Option("/data/powercontext-eval/codex-home/auth.json"),
    proxy_url: str = typer.Option("http://127.0.0.1:7890"),
    run_id: str | None = typer.Option(None),
) -> None:
    """Run Gold, PowerContext OFF/ON, official grading, and report generation."""

    from pathlib import Path

    catalog = SweBenchProCatalog.load(Path(dataset_path))
    result = run_swebench_pro_instance(
        RunConfig(
            root=Path(root_path),
            powercontext_source=Path(powercontext_source),
            powercontext_ref=powercontext_ref,
            harness_root=Path(harness_root),
            harness_python=Path(harness_python),
            codex_binary=Path(codex_bin),
            uv_binary=Path(uv_bin),
            auth_json=Path(auth_json),
            proxy_url=proxy_url,
            run_id=run_id or datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S"),
        ),
        instance=catalog.require(instance_id),
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
