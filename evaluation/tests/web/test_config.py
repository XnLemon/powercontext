from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from powercontext_eval.runner import INSTANCE_ID
from powercontext_eval.web.config import WebConfig
from powercontext_eval.web.models import FailureCategory, TaskCreate, TaskRecord, TaskStatus


def valid_task(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "powercontext_ref": "latest",
        "benchmark": "swebench-pro",
        "instance_id": INSTANCE_ID,
        "model": "gpt-5.6-sol",
        "reasoning_effort": "medium",
        "treatment_mode": "off_on",
        "idempotency_key": "request-1234",
    }
    payload.update(overrides)
    return payload


def test_web_config_derives_confined_paths(tmp_path: Path) -> None:
    config = WebConfig.for_root(tmp_path)

    assert config.database_path == tmp_path / "web" / "tasks.sqlite3"
    assert config.run_root == tmp_path
    assert config.frontend_dist.parts[-3:] == ("evaluation", "web", "dist")


def test_web_config_accepts_explicit_frontend_dist(tmp_path: Path) -> None:
    frontend_dist = tmp_path / "static"

    config = WebConfig.for_root(tmp_path, frontend_dist=frontend_dist)

    assert config.frontend_dist == frontend_dist


def test_web_config_from_environment_reads_only_named_variables(tmp_path: Path) -> None:
    frontend_dist = tmp_path / "frontend"
    environ = {
        "POWERCONTEXT_EVAL_ROOT": str(tmp_path),
        "POWERCONTEXT_EVAL_FRONTEND_DIST": str(frontend_dist),
        "POWERCONTEXT_EVAL_HOST": "127.0.0.2",
        "POWERCONTEXT_EVAL_PORT": "8123",
        "POWERCONTEXT_EVAL_LEASE_SECONDS": "90",
        "POWERCONTEXT_EVAL_POLL_SECONDS": "2.5",
        "ROOT": "/ignored",
        "PORT": "1",
        "PROXY_URL": "https://ignored.invalid",
    }

    config = WebConfig.from_environment(environ)

    assert config.root == tmp_path
    assert config.frontend_dist == frontend_dist
    assert config.host == "127.0.0.2"
    assert config.port == 8123
    assert config.lease_seconds == 90
    assert config.poll_seconds == 2.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("POWERCONTEXT_EVAL_ROOT", "relative"),
        ("POWERCONTEXT_EVAL_DATABASE_PATH", "relative.sqlite3"),
        ("POWERCONTEXT_EVAL_RUN_ROOT", "runs"),
        ("POWERCONTEXT_EVAL_FRONTEND_DIST", "dist"),
        ("POWERCONTEXT_EVAL_POWERCONTEXT_SOURCE", "source"),
        ("POWERCONTEXT_EVAL_HARNESS_ROOT", "harness"),
        ("POWERCONTEXT_EVAL_HARNESS_PYTHON", "python"),
        ("POWERCONTEXT_EVAL_RAW_SAMPLE_PATH", "sample.jsonl"),
        ("POWERCONTEXT_EVAL_CODEX_BINARY", "codex"),
        ("POWERCONTEXT_EVAL_UV_BINARY", "uv"),
        ("POWERCONTEXT_EVAL_AUTH_JSON", "auth.json"),
    ],
)
def test_web_config_rejects_relative_paths(tmp_path: Path, name: str, value: str) -> None:
    environ = {"POWERCONTEXT_EVAL_ROOT": str(tmp_path), name: value}

    with pytest.raises(ValidationError):
        WebConfig.from_environment(environ)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("POWERCONTEXT_EVAL_PORT", "0"),
        ("POWERCONTEXT_EVAL_PORT", "65536"),
        ("POWERCONTEXT_EVAL_LEASE_SECONDS", "0"),
        ("POWERCONTEXT_EVAL_POLL_SECONDS", "0"),
        ("POWERCONTEXT_EVAL_POLL_SECONDS", "31"),
    ],
)
def test_web_config_rejects_invalid_numeric_settings(tmp_path: Path, name: str, value: str) -> None:
    with pytest.raises(ValidationError):
        WebConfig.from_environment({"POWERCONTEXT_EVAL_ROOT": str(tmp_path), name: value})


def test_web_config_has_no_public_serialization_that_leaks_secrets(tmp_path: Path) -> None:
    secret = "https://user:secret@proxy.invalid"
    config = WebConfig.for_root(tmp_path, auth_json=tmp_path / "auth-secret.json", proxy_url=secret)

    assert not hasattr(config, "model_dump")
    assert not hasattr(config, "to_public")
    assert secret not in repr(config)
    assert "auth-secret.json" not in repr(config)


@pytest.mark.parametrize(
    "powercontext_ref",
    ["latest", "commit:0123456789abcdef0123456789abcdef01234567", "commit:ABCDEF0123456789ABCDEF0123456789ABCDEF01"],
)
def test_task_create_accepts_latest_or_exact_commit(powercontext_ref: str) -> None:
    request = TaskCreate.model_validate(valid_task(powercontext_ref=powercontext_ref))

    assert request.powercontext_ref == powercontext_ref


@pytest.mark.parametrize(
    "powercontext_ref",
    ["branch:main", "tag:v1", "main", "commit:0123456", "commit:" + "g" * 40],
)
def test_task_create_rejects_unsupported_revision(powercontext_ref: str) -> None:
    with pytest.raises(ValidationError):
        TaskCreate.model_validate(valid_task(powercontext_ref=powercontext_ref))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("benchmark", "swebench"),
        ("instance_id", "other-instance"),
        ("model", "gpt-5"),
        ("reasoning_effort", "high"),
        ("treatment_mode", "on"),
        ("idempotency_key", "unsafe key"),
        ("idempotency_key", "short"),
    ],
)
def test_task_create_rejects_values_outside_capabilities(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        TaskCreate.model_validate(valid_task(**{field: value}))


def test_task_record_exposes_only_safe_failure_details() -> None:
    record = TaskRecord(
        task_id="run-123",
        request=TaskCreate.model_validate(valid_task()),
        status=TaskStatus.FAILED,
        created_at=datetime(2026, 7, 29, tzinfo=UTC),
        finished_at=datetime(2026, 7, 29, 0, 1, tzinfo=UTC),
        failure_category=FailureCategory.CODEX_EXECUTION,
        failure_phase=None,
        failure_summary="Codex did not complete. Inspect retained m0 logs.",
    )

    assert record.failure_category is FailureCategory.CODEX_EXECUTION
    assert record.failure_summary == "Codex did not complete. Inspect retained m0 logs."
