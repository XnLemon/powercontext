from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from powercontext_eval.benchmarks.swebench_pro.evaluator import OfficialEvaluation
from powercontext_eval.codex import CodexOutcome
from powercontext_eval.models import Arm
from powercontext_eval.report import ReportBundle
from powercontext_eval.runner import (
    INSTANCE_ID,
    MinimalRunConfig,
    MinimalRunResult,
    PhaseCallback,
    RunPhase,
    run_minimal_swebench_pro,
)


def test_run_phases_have_stable_order_and_values() -> None:
    assert list(RunPhase) == [
        RunPhase.PREPARING,
        RunPhase.VALIDATING_GOLD,
        RunPhase.RUNNING_OFF,
        RunPhase.RUNNING_ON,
        RunPhase.OFFICIAL_EVALUATION,
        RunPhase.GENERATING_REPORT,
    ]
    assert [phase.value for phase in RunPhase] == [
        "preparing",
        "validating_gold",
        "running_off",
        "running_on",
        "official_evaluation",
        "generating_report",
    ]


def _config(tmp_path: Path) -> MinimalRunConfig:
    raw_sample_path = tmp_path / "instance.jsonl"
    raw_sample_path.write_text("{}\n")
    auth_json = tmp_path / "auth.json"
    auth_json.write_text('{"api_key":"runner-secret-value"}')
    return MinimalRunConfig(
        root=tmp_path / "eval",
        powercontext_source=tmp_path / "source",
        powercontext_ref="latest",
        harness_root=tmp_path / "harness",
        harness_python=tmp_path / "python",
        raw_sample_path=raw_sample_path,
        codex_binary=tmp_path / "codex",
        uv_binary=tmp_path / "uv",
        auth_json=auth_json,
        proxy_url="http://127.0.0.1:7890",
        run_id="run-test",
    )


def _run_with_fakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[object],
    on_phase: PhaseCallback | None = None,
) -> tuple[MinimalRunConfig, MinimalRunResult]:
    config = _config(tmp_path)
    materialized = tmp_path / "materialized"
    materialized.mkdir()
    resolved = SimpleNamespace(sha="a" * 40)
    instance = SimpleNamespace(
        instance_id=INSTANCE_ID,
        patch="gold patch",
        base_commit="base",
        codex_prompt=lambda: "prompt",
    )

    class FakeProcess:
        def run(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(stdout="candidate patch")

    class FakeSource:
        def resolve(self, *args: object, **kwargs: object) -> object:
            events.append("prepare")
            return resolved

        def materialize(self, *args: object, **kwargs: object) -> Path:
            return materialized

    class FakeEvaluator:
        def evaluate(self, **kwargs: object) -> OfficialEvaluation:
            prediction_path = kwargs["prediction_path"]
            assert isinstance(prediction_path, Path)
            events.append("gold" if prediction_path.parent.name == "gold" else "official")
            return OfficialEvaluation(INSTANCE_ID, True, "", "")

    class FakeSut:
        def run_pair(
            self,
            *args: object,
            before_arm: Callable[[Arm], None] | None = None,
            **kwargs: object,
        ) -> dict[Arm, object]:
            assert before_arm is not None
            for arm in (Arm.OFF, Arm.ON):
                before_arm(arm)
                events.append(arm)
            outcome = SimpleNamespace(codex=CodexOutcome("", None))
            return {Arm.OFF: outcome, Arm.ON: outcome}

    monkeypatch.setattr("powercontext_eval.runner.ProcessRunner", FakeProcess)
    monkeypatch.setattr("powercontext_eval.runner.GitSource", lambda **kwargs: FakeSource())
    monkeypatch.setattr("powercontext_eval.runner.OfficialEvaluator", lambda *args, **kwargs: FakeEvaluator())
    monkeypatch.setattr("powercontext_eval.runner.DockerSut", lambda *args, **kwargs: FakeSut())
    monkeypatch.setattr(
        "powercontext_eval.runner.SweBenchProInstance.from_raw",
        lambda *args, **kwargs: instance,
    )
    callback = on_phase if on_phase is not None else lambda phase: events.append(phase)
    result = run_minimal_swebench_pro(config, on_phase=callback)
    return config, result


def test_runner_emits_phases_immediately_before_named_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []

    _run_with_fakes(tmp_path, monkeypatch, events)

    assert events == [
        RunPhase.PREPARING,
        "prepare",
        RunPhase.VALIDATING_GOLD,
        "gold",
        RunPhase.RUNNING_OFF,
        Arm.OFF,
        RunPhase.RUNNING_ON,
        Arm.ON,
        RunPhase.OFFICIAL_EVALUATION,
        "official",
        "official",
        RunPhase.GENERATING_REPORT,
    ]


def test_runner_preserves_falsey_phase_callback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FalseyPhaseRecorder:
        def __init__(self) -> None:
            self.phases: list[RunPhase] = []

        def __bool__(self) -> bool:
            return False

        def __call__(self, phase: RunPhase) -> None:
            self.phases.append(phase)

    recorder = FalseyPhaseRecorder()

    _run_with_fakes(tmp_path, monkeypatch, [], on_phase=recorder)

    assert recorder.phases == list(RunPhase)


def test_runner_persists_strict_validated_report_json_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, result = _run_with_fakes(tmp_path, monkeypatch, [])

    report_data = json.loads((config.root / "runs" / result.run_id / "report.json").read_text())
    report = ReportBundle.model_validate(report_data, strict=True)
    assert report.model_dump(mode="json") == report_data
    retained = json.dumps(report_data, sort_keys=True)
    assert "api_key" not in retained
    assert "runner-secret-value" not in retained
