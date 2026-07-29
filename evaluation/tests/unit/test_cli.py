import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from powercontext_eval.cli import app
from powercontext_eval.runner import MinimalRunResult


def test_cli_help_describes_the_evaluation_runner() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "PowerContext evaluation runner" in result.output
    assert not isinstance(result.exception, RuntimeError)


def test_codex_contract_smoke_is_an_executable_injectable_cli(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_contract_smoke(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"off_prompt_sources": 0, "on_prompt_sources": 1, "status": "passed"}

    monkeypatch.setattr("powercontext_eval.cli.run_codex_contract_smoke", fake_contract_smoke)
    result = CliRunner().invoke(
        app,
        [
            "codex-contract-smoke",
            "--run-root",
            "/tmp/contract",
            "--task-image",
            "fixture:image",
            "--codex-bin",
            "/tools/codex",
            "--uv-bin",
            "/tools/uv",
            "--powercontext-source",
            "/source",
            "--powercontext-sha",
            "a" * 40,
            "--auth-json",
            "/auth.json",
            "--proxy-url",
            "http://127.0.0.1:7890",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"status": "passed"' in result.output
    assert calls == [
        {
            "run_root": "/tmp/contract",
            "task_image": "fixture:image",
            "codex_bin": "/tools/codex",
            "uv_bin": "/tools/uv",
            "powercontext_source": "/source",
            "powercontext_sha": "a" * 40,
            "auth_json": "/auth.json",
            "proxy_url": "http://127.0.0.1:7890",
            "prompt": "Reply with exactly OK.",
        }
    ]


def test_cli_module_is_directly_executable() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "powercontext_eval.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "codex-contract-smoke" in result.stdout


def test_swebench_pro_run_exposes_the_minimal_m0_command(monkeypatch) -> None:
    calls: list[object] = []
    instance = object()

    def fake_run(config: object, *, instance: object) -> MinimalRunResult:
        calls.append((config, instance))
        return MinimalRunResult("run-fixed", Path("/data/powercontext-eval/runs/run-fixed/report.md"), False, True)

    class FakeCatalog:
        def require(self, instance_id: str) -> object:
            assert instance_id == "instance_owner__repo-b"
            return instance

    monkeypatch.setattr(
        "powercontext_eval.cli.SweBenchProCatalog.load",
        lambda path: FakeCatalog(),
    )
    monkeypatch.setattr("powercontext_eval.cli.run_swebench_pro_instance", fake_run)
    result = CliRunner().invoke(
        app,
        ["swebench-pro", "run", "--run-id", "run-fixed", "--instance-id", "instance_owner__repo-b"],
    )

    assert result.exit_code == 0, result.output
    assert '"run_id": "run-fixed"' in result.output
    assert '"off_resolved": false' in result.output
    assert '"on_resolved": true' in result.output
    assert len(calls) == 1
    assert calls[0][1] is instance
