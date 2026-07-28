"""Adapter for the pinned official SWE-bench Pro evaluator."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from powercontext_eval.errors import PowerContextEvalError
from powercontext_eval.process import ProcessRunner


class OfficialResultError(PowerContextEvalError):
    """Official evaluator output is absent or ambiguous."""


@dataclass(frozen=True)
class OfficialEvaluation:
    """Strict official outcome and raw process evidence."""

    instance_id: str
    resolved: bool
    raw_stdout: str
    raw_stderr: str


class OfficialEvaluator:
    """Invoke, but never reimplement, the official evaluator."""

    def __init__(self, runner: ProcessRunner, *, python_executable: str) -> None:
        self._runner = runner
        self._python = python_executable

    def evaluate(
        self,
        *,
        harness_root: Path,
        raw_sample_path: Path,
        prediction_path: Path,
        output_dir: Path,
        instance_id: str,
    ) -> OfficialEvaluation:
        output_dir.mkdir(parents=True, exist_ok=True)
        argv = (
            self._python,
            "swe_bench_pro_eval.py",
            "--raw_sample_path",
            str(raw_sample_path),
            "--patch_path",
            str(prediction_path),
            "--output_dir",
            str(output_dir),
            "--dockerhub_username",
            "jefzda",
            "--scripts_dir",
            str(harness_root / "run_scripts"),
            "--num_workers",
            "1",
            "--use_local_docker",
            "--docker_platform",
            "linux/amd64",
            "--redo",
            "--block_network",
        )
        environment = {}
        if "FAKE_EVAL_RESULT" in os.environ:
            environment["FAKE_EVAL_RESULT"] = os.environ["FAKE_EVAL_RESULT"]
        result = self._runner.run(
            argv,
            cwd=harness_root,
            timeout=4_200,
            env=environment,
        )
        (output_dir / "evaluator.stdout.log").write_text(result.stdout)
        (output_dir / "evaluator.stderr.log").write_text(result.stderr)
        result_path = output_dir / "eval_results.json"
        try:
            payload = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise OfficialResultError("Official result is missing or malformed") from error
        if not isinstance(payload, dict) or instance_id not in payload:
            raise OfficialResultError("Official result is missing the requested instance")
        if set(payload) != {instance_id}:
            raise OfficialResultError("Official result must contain the exact instance")
        resolved = payload[instance_id]
        if type(resolved) is not bool:
            raise OfficialResultError("Official result must be a boolean")
        return OfficialEvaluation(instance_id, resolved, result.stdout, result.stderr)
