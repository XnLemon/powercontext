"""Browser-test service using production batch API, store, worker, and frontend."""

from __future__ import annotations

import json
import shutil
import signal
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Literal

import uvicorn

from powercontext_eval.artifacts import ArmState
from powercontext_eval.benchmarks.swebench_pro.adapter import (
    DATASET_REVISION,
    HARNESS_COMMIT,
    SweBenchProInstance,
)
from powercontext_eval.models import Arm
from powercontext_eval.paths import EvaluationPaths
from powercontext_eval.report import ArmReport, MetricSet, ReportBundle, TestGroupReport, render_report
from powercontext_eval.runner import MinimalRunResult, PhaseCallback, RunConfig, RunPhase
from powercontext_eval.web.api import create_app
from powercontext_eval.web.config import WebConfig
from powercontext_eval.web.store import TaskStore
from powercontext_eval.web.worker import EvaluationWorker

PORT = 4177
PHASE_DELAY_SECONDS = 0.05
PLUGIN_SHA = "a" * 40


class _SignalManagedServer(uvicorn.Server):
    """Leave signal ownership with the fixture so temporary state is removed."""

    def install_signal_handlers(self) -> None:
        return


class _Catalog:
    instance_ids = tuple(f"instance_e2e__repo-{letter}" for letter in "abcdef")

    def __init__(self) -> None:
        self._instances = {
            instance_id: SweBenchProInstance(
                repo=f"e2e/repo-{letter}",
                instance_id=instance_id,
                base_commit=letter * 40,
                patch="",
                test_patch=f"diff --git a/tests/test_{letter}.py b/tests/test_{letter}.py\n",
                problem_statement=f"Fix the complete deterministic browser-test problem for repository {letter}.",
                fail_to_pass=(f"test_fix_{letter}",),
                pass_to_pass=(f"test_regression_{letter}",),
                before_repo_set_cmd="",
                selected_test_files_to_run=json.dumps([f"tests/test_{letter}.py"]),
                task_image=f"fixture/task:{letter}",
                raw_row=MappingProxyType({}),
            )
            for letter, instance_id in zip("abcdef", self.instance_ids, strict=True)
        }

    def require(self, instance_id: str) -> SweBenchProInstance:
        return self._instances[instance_id]


class _Source:
    def resolve(self, _source: str | Path, _requested: object) -> object:
        return SimpleNamespace(sha=PLUGIN_SHA)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _arm(
    name: Literal["off", "on"],
    *,
    resolved: bool,
    input_tokens: int,
    output_tokens: int,
    test_name: str,
) -> ArmReport:
    failed = () if resolved else (test_name,)
    return ArmReport(
        arm=name,
        state=ArmState.TREATMENT_VALIDATED,
        resolved=resolved,
        passed=resolved,
        treatment_valid=True,
        patch_applied=True,
        fail_to_pass=TestGroupReport(
            passed=1 if resolved else 0,
            total=1,
            failed=failed,
        ),
        pass_to_pass=TestGroupReport(passed=1, total=1),
        log_excerpt=None if resolved else f"{test_name} failed",
        metrics=MetricSet(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _evidence(run_id: str, arm: str) -> dict[str, object]:
    enabled = arm == "on"
    return {
        "mcp_requests": 2 if enabled else 0,
        "prompt_sources": 2 if enabled else 0,
        "plugin_checkout_sha": PLUGIN_SHA,
        "plugin_id": "powercontext@powercontext",
        "plugin_installed": True,
        "plugin_version": "0.1.0",
        "scope_id": f"eval:{run_id}:{arm}",
        "server_ready": True,
    }


def _timeline(
    layout: EvaluationPaths,
    *,
    arm: Literal["off", "on"],
    instance: SweBenchProInstance,
    resolved: bool,
) -> None:
    values: list[dict[str, object]] = [
        {
            "sequence": 1,
            "observed_at": "2026-07-29T08:10:11.100000Z",
            "elapsed_ms": 0,
            "arm": arm,
            "actor": "benchmark",
            "event_type": "benchmark_prompt",
            "input": {"prompt": instance.codex_prompt()},
            "output": None,
            "source_artifact": "instance.jsonl",
            "source_sequence": 0,
        },
        {
            "sequence": 2,
            "observed_at": "2026-07-29T08:10:11.200000Z",
            "elapsed_ms": 100,
            "arm": arm,
            "actor": "codex",
            "event_type": "agent_message",
            "input": None,
            "output": {"event": {"type": "agent_message", "message": "Inspect repository structure."}},
            "source_artifact": "context/codex-observed.jsonl",
            "source_sequence": 1,
        },
    ]
    if arm == "on":
        values.extend(
            [
                {
                    "sequence": 3,
                    "observed_at": "2026-07-29T08:10:11.300000Z",
                    "elapsed_ms": 200,
                    "arm": arm,
                    "actor": "powercontext",
                    "event_type": "powercontext_injection",
                    "input": {
                        "query": f"{instance.repo} architecture",
                        "scope_id": f"eval:{layout.run_id}:on",
                        "session_id": "e2e-session",
                        "turn_id": "turn-1",
                    },
                    "output": {
                        "hits": [
                            {
                                "citation": "memory://architecture/1",
                                "text": "Use the repository service boundary.",
                                "score": 0.93,
                            }
                        ],
                        "injected_text": "PowerContext recalled the repository service boundary.",
                    },
                    "source_artifact": "context/powercontext-injections.jsonl",
                    "source_sequence": 1,
                },
                {
                    "sequence": 4,
                    "observed_at": "2026-07-29T08:10:11.400000Z",
                    "elapsed_ms": 300,
                    "arm": arm,
                    "actor": "powercontext",
                    "event_type": "powercontext_injection",
                    "input": {
                        "query": f"{instance.repo} test history",
                        "scope_id": f"eval:{layout.run_id}:on",
                        "session_id": "e2e-session",
                        "turn_id": "turn-2",
                    },
                    "output": {
                        "hits": [
                            {
                                "citation": "memory://test/2",
                                "text": "The regression test requires the legacy behavior.",
                                "score": 0.88,
                            }
                        ],
                        "injected_text": "PowerContext recalled the exact regression constraint.",
                    },
                    "source_artifact": "context/powercontext-injections.jsonl",
                    "source_sequence": 2,
                },
            ]
        )
    values.append(
        {
            "sequence": len(values) + 1,
            "observed_at": "2026-07-29T08:12:00.000000Z",
            "elapsed_ms": 108_900,
            "arm": arm,
            "actor": "official_evaluator",
            "event_type": "official_evaluation",
            "input": {"instance_id": instance.instance_id},
            "output": {
                "resolved": resolved,
                "patch_applied": True,
                "fail_to_pass": {
                    "passed": 1 if resolved else 0,
                    "total": 1,
                    "failed": [] if resolved else [instance.fail_to_pass[0]],
                },
                "pass_to_pass": {"passed": 1, "total": 1, "failed": []},
                "log_excerpt": None if resolved else f"{instance.fail_to_pass[0]} failed",
            },
            "source_artifact": "official",
            "source_sequence": 1,
        }
    )
    timeline_path = layout.arm_artifacts(Arm(arm)) / "context" / "timeline.jsonl"
    timeline_path.parent.mkdir(parents=True, exist_ok=True)
    timeline_path.write_text(
        "".join(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n" for value in values),
        encoding="utf-8",
    )


OUTCOMES: dict[str, tuple[bool, bool, tuple[int, int], tuple[int, int]]] = {
    "a": (False, True, (100, 10), (90, 9)),
    "b": (True, False, (80, 8), (120, 12)),
    "c": (True, True, (110, 11), (105, 10)),
    "d": (False, False, (70, 7), (72, 8)),
    "f": (True, True, (95, 9), (88, 8)),
}


def fake_runner(
    config: RunConfig,
    *,
    instance: SweBenchProInstance,
    on_phase: PhaseCallback,
) -> MinimalRunResult:
    """Emit real phases and retain deterministic per-task report and context artifacts."""

    if config.run_id is None:
        raise ValueError("The browser worker must provide a run ID")
    for phase in RunPhase:
        on_phase(phase)
        time.sleep(PHASE_DELAY_SECONDS)
    letter = instance.instance_id[-1]
    if letter == "e":
        raise RuntimeError("deterministic execution failure")
    off_resolved, on_resolved, off_usage, on_usage = OUTCOMES[letter]
    layout = EvaluationPaths(config.root, config.run_id)
    layout.run_artifacts.mkdir(parents=True)
    bundle = ReportBundle(
        title="PowerContext browser batch evaluation",
        revisions={
            "dataset": DATASET_REVISION,
            "harness": HARNESS_COMMIT,
            "powercontext": PLUGIN_SHA,
        },
        configuration={
            "codex": "0.145.0",
            "instance": instance.instance_id,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
        },
        off=_arm(
            "off",
            resolved=off_resolved,
            input_tokens=off_usage[0],
            output_tokens=off_usage[1],
            test_name=instance.fail_to_pass[0],
        ),
        on=_arm(
            "on",
            resolved=on_resolved,
            input_tokens=on_usage[0],
            output_tokens=on_usage[1],
            test_name=instance.fail_to_pass[0],
        ),
    )
    _write_json(layout.run_artifacts / "report.json", bundle.model_dump(mode="json"))
    (layout.run_artifacts / "report.md").write_text(render_report(bundle), encoding="utf-8")
    for arm, resolved in ((Arm.OFF, off_resolved), (Arm.ON, on_resolved)):
        _write_json(
            layout.arm_artifacts(arm) / "powercontext" / "treatment.json",
            _evidence(config.run_id, arm.value),
        )
        _timeline(layout, arm=arm.value, instance=instance, resolved=resolved)
    return MinimalRunResult(config.run_id, layout.run_artifacts / "report.md", off_resolved, on_resolved)


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
        poll_seconds=0.02,
        lease_seconds=10,
        port=PORT,
    )
    store = TaskStore(config.database_path, lease_duration=timedelta(seconds=config.lease_seconds))
    store.initialize()
    catalog = _Catalog()
    worker = EvaluationWorker(
        config,
        store,
        runner=fake_runner,
        source=_Source(),
        catalog=catalog,
        worker_id="browser-e2e-worker",
    )
    worker_thread = threading.Thread(target=worker.run_forever, daemon=True, name="browser-e2e-worker")
    worker_thread.start()
    server = _SignalManagedServer(
        uvicorn.Config(
            create_app(config, store, catalog=catalog),
            host="127.0.0.1",
            port=PORT,
            log_level="warning",
            access_log=False,
        )
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
