import signal
from pathlib import Path

from typer.testing import CliRunner

from powercontext_eval.cli import _request_worker_stop, app
from powercontext_eval.web.config import WebConfig


def test_top_level_help_exposes_service_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "web" in result.output
    assert "worker" in result.output


def test_web_builds_config_from_cli_root_and_environment(monkeypatch, tmp_path: Path) -> None:
    calls: dict[str, object] = {}
    application = object()

    monkeypatch.setenv("POWERCONTEXT_EVAL_PORT", "8123")
    monkeypatch.setenv("POWERCONTEXT_EVAL_HOST", "127.0.0.2")

    def fake_create_app(config: object) -> object:
        calls["config"] = config
        return application

    monkeypatch.setattr("powercontext_eval.web.api.create_app", fake_create_app)

    def fake_run(app: object, *, host: str, port: int) -> None:
        calls.update(app=app, host=host, port=port)

    monkeypatch.setattr("uvicorn.run", fake_run)

    result = CliRunner().invoke(app, ["web", "--root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    config = calls["config"]
    assert isinstance(config, WebConfig)
    assert config.root == tmp_path
    assert calls == {"config": config, "app": application, "host": "127.0.0.2", "port": 8123}


def test_worker_initializes_store_and_runs_with_configured_poll(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeStore:
        def __init__(self, database: Path, *, lease_duration: object) -> None:
            calls.append(("store", database, lease_duration))

        def initialize(self) -> None:
            calls.append(("initialize",))

    class FakeWorker:
        def __init__(self, config: object, store: object, *, usage_probe: object) -> None:
            calls.append(("worker", config, store, usage_probe))

        def run_forever(self) -> None:
            calls.append(("run_forever",))

        def stop(self) -> None:
            calls.append(("stop",))

    monkeypatch.setenv("POWERCONTEXT_EVAL_POLL_SECONDS", "2.5")
    monkeypatch.setenv("POWERCONTEXT_EVAL_LEASE_SECONDS", "90")
    monkeypatch.setattr("powercontext_eval.web.store.TaskStore", FakeStore)
    monkeypatch.setattr("powercontext_eval.web.worker.EvaluationWorker", FakeWorker)
    monkeypatch.setattr("signal.getsignal", lambda _signal: signal.SIG_DFL)
    monkeypatch.setattr("signal.signal", lambda *args: calls.append(("signal", *args)))

    result = CliRunner().invoke(app, ["worker", "--root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert calls[0][0] == "store"
    assert calls[1] == ("initialize",)
    assert calls[2][0] == "worker"
    assert isinstance(calls[2][1], WebConfig)
    assert ("run_forever",) in calls
    assert calls[2][1].poll_seconds == 2.5
    assert ("stop",) not in calls


def test_signal_callback_requests_graceful_worker_stop() -> None:
    calls: list[str] = []

    class Worker:
        def stop(self) -> None:
            calls.append("stop")

    _request_worker_stop(Worker(), signal.SIGTERM, None)

    assert calls == ["stop"]


def test_invalid_configuration_is_concise_and_does_not_print_secrets(monkeypatch) -> None:
    secret = "https://user:secret@proxy.invalid"
    monkeypatch.setenv("POWERCONTEXT_EVAL_ROOT", "relative")
    monkeypatch.setenv("POWERCONTEXT_EVAL_PROXY_URL", secret)

    result = CliRunner().invoke(app, ["web"])

    assert result.exit_code == 2
    assert "Invalid evaluation configuration" in result.output
    assert secret not in result.output
    assert "validation error" not in result.output.casefold()
