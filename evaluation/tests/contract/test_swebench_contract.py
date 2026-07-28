from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from powercontext_eval.benchmarks.base import GoldCheckFailed, GoldResult, run_after_gold
from powercontext_eval.benchmarks.swebench_pro.adapter import (
    DATASET_REVISION,
    HARNESS_COMMIT,
    DatasetSchemaError,
    SweBenchProInstance,
)
from powercontext_eval.benchmarks.swebench_pro.evaluator import (
    OfficialEvaluator,
    OfficialResultError,
)
from powercontext_eval.benchmarks.swebench_pro.prediction import BinaryPatchError, encode_predictions
from powercontext_eval.process import ProcessRunner

INSTANCE_ID = "instance_flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9"
DATASET_FIELDS = {
    "repo",
    "instance_id",
    "base_commit",
    "patch",
    "test_patch",
    "problem_statement",
    "requirements",
    "interface",
    "repo_language",
    "fail_to_pass",
    "pass_to_pass",
    "issue_specificity",
    "issue_categories",
    "before_repo_set_cmd",
    "selected_test_files_to_run",
    "dockerhub_tag",
}


def raw_instance() -> dict[str, object]:
    return {
        "repo": "flipt-io/flipt",
        "instance_id": INSTANCE_ID,
        "base_commit": "0018c5df774444117b107dfe3fe503d4c7126d73",
        "patch": "diff --git a/gold b/gold\n",
        "test_patch": "diff --git a/hidden b/hidden\n",
        "problem_statement": "parse CORS origins",
        "requirements": "split whitespace-separated values",
        "interface": "No new interfaces.",
        "repo_language": "go",
        "fail_to_pass": '["TestLoad"]',
        "pass_to_pass": "[]",
        "issue_specificity": '["regression_bug"]',
        "issue_categories": '["back_end_knowledge"]',
        "before_repo_set_cmd": "git reset --hard",
        "selected_test_files_to_run": '["TestLoad"]',
        "dockerhub_tag": "flipt-io.flipt-flipt-io__flipt-518ec324b66a07fdd95464a5e9ca5fe7681ad8f9",
    }


def test_pins_public_harness_and_dataset() -> None:
    assert HARNESS_COMMIT == "ca10a60a5fcae51e6948ffe1485d4153d421e6c5"
    assert DATASET_REVISION == "7ab5114912baf22bb098818e604c02fe7ad2c11f"


def test_instance_requires_exact_pinned_dataset_schema_and_manifest_digest() -> None:
    assert set(raw_instance()) == DATASET_FIELDS
    instance = SweBenchProInstance.from_raw(raw_instance(), docker_manifest_digest="sha256:" + "a" * 64)
    assert instance.docker_manifest_digest == "sha256:" + "a" * 64

    missing = raw_instance()
    missing.pop("requirements")
    with pytest.raises(DatasetSchemaError, match="missing.*requirements"):
        SweBenchProInstance.from_raw(missing, docker_manifest_digest="sha256:" + "a" * 64)

    extra = raw_instance()
    extra["unknown"] = "value"
    with pytest.raises(DatasetSchemaError, match="unexpected.*unknown"):
        SweBenchProInstance.from_raw(extra, docker_manifest_digest="sha256:" + "a" * 64)

    with pytest.raises(DatasetSchemaError, match="manifest digest"):
        SweBenchProInstance.from_raw(raw_instance(), docker_manifest_digest="")


def test_prompt_exposes_only_public_task_fields() -> None:
    instance = SweBenchProInstance.from_raw(raw_instance(), docker_manifest_digest="sha256:" + "a" * 64)
    prompt = instance.codex_prompt()
    assert instance.problem_statement in prompt
    assert instance.requirements in prompt
    assert instance.interface in prompt
    assert instance.patch not in prompt
    assert instance.test_patch not in prompt
    assert instance.fail_to_pass not in prompt
    assert instance.selected_test_files_to_run not in prompt


def test_prediction_is_official_json_array_and_preserves_patch_bytes() -> None:
    patch = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\r\n+new\n"
    encoded = encode_predictions(INSTANCE_ID, patch, "codex-0.145.0")
    assert json.loads(encoded) == [{"instance_id": INSTANCE_ID, "patch": patch, "prefix": "codex-0.145.0"}]
    assert encoded.encode().decode() == encoded


@pytest.mark.parametrize("marker", ["GIT binary patch", "Binary files a/x and b/x differ"])
def test_prediction_rejects_binary_patch(marker: str) -> None:
    with pytest.raises(BinaryPatchError):
        encode_predictions(INSTANCE_ID, f"diff --git a/x b/x\n{marker}\n", "codex-0.145.0")


def test_gold_failure_prevents_arm_factory_from_being_called() -> None:
    called = False

    def arms() -> None:
        nonlocal called
        called = True

    with pytest.raises(GoldCheckFailed):
        run_after_gold(GoldResult(instance_id=INSTANCE_ID, resolved=False), arms)
    assert not called


def test_official_evaluator_uses_exact_cli_and_retains_raw_output(tmp_path: Path) -> None:
    harness = tmp_path / "harness"
    harness.mkdir()
    fake = Path(__file__).parent / "fixtures" / "fake_evaluator.py"
    (harness / "swe_bench_pro_eval.py").write_bytes(fake.read_bytes())
    (harness / "run_scripts").mkdir()
    raw_path = tmp_path / "instance.jsonl"
    raw_path.write_text(json.dumps(raw_instance(), separators=(",", ":")) + "\n")
    prediction_path = tmp_path / "predictions.json"
    prediction_path.write_text(encode_predictions(INSTANCE_ID, "diff --git a/a b/a\n", "codex-0.145.0"))
    output_dir = tmp_path / "output"

    result = OfficialEvaluator(ProcessRunner(), python_executable=sys.executable).evaluate(
        harness_root=harness,
        raw_sample_path=raw_path,
        prediction_path=prediction_path,
        output_dir=output_dir,
        instance_id=INSTANCE_ID,
    )

    assert result.resolved is True
    assert "FAKE OFFICIAL EVALUATOR" in result.raw_stdout
    invocation = json.loads((output_dir / "invocation.json").read_text())
    assert invocation == {
        "raw_sample_path": str(raw_path),
        "patch_path": str(prediction_path),
        "output_dir": str(output_dir),
        "dockerhub_username": "jefzda",
        "scripts_dir": str(harness / "run_scripts"),
        "num_workers": "1",
        "use_local_docker": True,
        "docker_platform": "linux/amd64",
        "redo": True,
        "block_network": True,
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "missing"),
        ({INSTANCE_ID: 1}, "boolean"),
        ({INSTANCE_ID: True, "other": False}, "exact instance"),
    ],
)
def test_official_evaluator_rejects_non_exact_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], message: str
) -> None:
    harness = tmp_path / "harness"
    harness.mkdir()
    fake = Path(__file__).parent / "fixtures" / "fake_evaluator.py"
    (harness / "swe_bench_pro_eval.py").write_bytes(fake.read_bytes())
    (harness / "run_scripts").mkdir()
    raw_path = tmp_path / "instance.jsonl"
    raw_path.write_text("{}\n")
    prediction_path = tmp_path / "predictions.json"
    prediction_path.write_text("[]")
    output_dir = tmp_path / "output"
    monkeypatch.setenv("FAKE_EVAL_RESULT", json.dumps(payload))

    with pytest.raises(OfficialResultError, match=message):
        OfficialEvaluator(ProcessRunner(), python_executable=sys.executable).evaluate(
            harness_root=harness,
            raw_sample_path=raw_path,
            prediction_path=prediction_path,
            output_dir=output_dir,
            instance_id=INSTANCE_ID,
        )
