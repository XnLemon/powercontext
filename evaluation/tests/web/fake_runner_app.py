"""Browser-test service using the production API, store, worker, and frontend."""

from __future__ import annotations

import json
import shutil
import signal
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Literal

import uvicorn

from powercontext_eval.artifacts import ArmState
from powercontext_eval.models import Arm
from powercontext_eval.paths import EvaluationPaths
from powercontext_eval.report import ArmReport, MetricSet, ReportBundle, render_report
from powercontext_eval.runner import MinimalRunConfig, MinimalRunResult, PhaseCallback, RunPhase
from powercontext_eval.web.api import create_app
from powercontext_eval.web.config import WebConfig
from powercontext_eval.web.store import TaskStore
from powercontext_eval.web.worker import EvaluationWorker

PORT = 4177
PHASE_DELAY_SECONDS = 0.35
PLUGIN_SHA = "a" * 40


class _SignalManagedServer(uvicorn.Server):
    """Leave signal ownership with the fixture so temporary state is removed."""

    def install_signal_handlers(self) -> None:
        return


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _arm(
    name: Literal["off", "on"],
    *,
    input_tokens: int,
    output_tokens: int,
    elapsed_seconds: float,
    patch_bytes: int,
) -> ArmReport:
    return ArmReport(
        arm=name,
        state=ArmState.TREATMENT_VALIDATED,
        resolved=True,
        passed=True,
        treatment_valid=True,
        metrics=MetricSet(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            elapsed_seconds=elapsed_seconds,
            patch_bytes=patch_bytes,
        ),
    )


def _evidence(run_id: str, arm: str) -> dict[str, object]:
    enabled = arm == "on"
    return {
        "mcp_requests": 3 if enabled else 0,
        "prompt_sources": 2 if enabled else 0,
        "plugin_checkout_sha": PLUGIN_SHA,
        "plugin_id": "powercontext@powercontext",
        "plugin_installed": True,
        "plugin_version": "0.1.0",
        "scope_id": f"eval:{run_id}:{arm}",
        "server_ready": True,
    }


def fake_runner(config: MinimalRunConfig, *, on_phase: PhaseCallback) -> MinimalRunResult:
    """Emit real phases and retain a strictly valid deterministic report."""
    if config.run_id is None:
        raise ValueError("The browser worker must provide a run ID")
    for phase in RunPhase:
        on_phase(phase)
        time.sleep(PHASE_DELAY_SECONDS)

    layout = EvaluationPaths(config.root, config.run_id)
    layout.run_artifacts.mkdir(parents=True)
    bundle = ReportBundle(
        title="PowerContext browser acceptance",
        revisions={"powercontext": PLUGIN_SHA, "benchmark": "fake-pinned"},
        configuration={
            "model": "gpt-5.6-sol",
            "plugin_id": "powercontext@powercontext",
            "plugin_version": "0.1.0",
            "treatment_mode": "off_on",
        },
        off=_arm("off", input_tokens=1200, output_tokens=240, elapsed_seconds=4.25, patch_bytes=128),
        on=_arm("on", input_tokens=1000, output_tokens=220, elapsed_seconds=3.75, patch_bytes=112),
    )
    _write_json(layout.run_artifacts / "report.json", bundle.model_dump(mode="json"))
    (layout.run_artifacts / "report.md").write_text(render_report(bundle), encoding="utf-8")
    for arm in (Arm.OFF, Arm.ON):
        _write_json(
            layout.arm_artifacts(arm) / "powercontext" / "treatment.json",
            _evidence(config.run_id, arm.value),
        )

    return MinimalRunResult(config.run_id, layout.run_artifacts / "report.md", True, True)


def main() -> None:
    repository = Path(__file__).resolve().parents[3]
    temporary_root = Path(tempfile.mkdtemp(prefix="powercontext-e2e-"))
    frontend = temporary_root / "deploy" / "powercontext" / "evaluation" / "web" / "dist"
    shutil.copytree(repository / "evaluation" / "web" / "dist", frontend)
    config = WebConfig.for_root(
        temporary_root,
        database_path=temporary_root / "web" / "tasks.sqlite3",
        run_root=temporary_root,
        frontend_dist=frontend,
        poll_seconds=0.03,
        lease_seconds=10,
        port=PORT,
    )
    store = TaskStore(config.database_path, lease_duration=timedelta(seconds=config.lease_seconds))
    store.initialize()
    worker = EvaluationWorker(config, store, runner=fake_runner, worker_id="browser-e2e-worker")
    worker_thread = threading.Thread(target=worker.run_forever, daemon=True, name="browser-e2e-worker")
    worker_thread.start()
    server = _SignalManagedServer(
        uvicorn.Config(create_app(config, store), host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    )

    def stop(_signal: int, _frame: object) -> None:
        server.should_exit = True
        worker.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"E2E_TEMP_ROOT={temporary_root}", flush=True)
    try:
        server.run()
    finally:
        worker.stop()
        worker_thread.join(timeout=15)
        shutil.rmtree(temporary_root)
        print(f"E2E_TEMP_ROOT_REMOVED={temporary_root}", flush=True)


if __name__ == "__main__":
    main()
