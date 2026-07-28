from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from powercontext_eval.artifacts import ArtifactStore
from powercontext_eval.codex import (
    CodexInfrastructureError,
    CodexInvocation,
    CodexRunner,
    UnsafeCodexInvocation,
)
from powercontext_eval.models import Arm
from powercontext_eval.powercontext_sut import (
    ArmPaths,
    ContainerLimits,
    DockerSut,
    InvalidTreatment,
    ProxyRelayConfig,
    SocatProxyRelay,
    SutConfig,
    TreatmentEvidence,
    UnsafeSutConfiguration,
    validate_treatment,
)
from powercontext_eval.process import CommandFailed, CommandResult, CommandTimedOut

EXPECTED_COMMON = (
    "codex",
    "exec",
    "--ephemeral",
    "--ignore-rules",
    "--json",
    "--disable",
    "shell_snapshot",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--model",
    "gpt-5.6-sol",
    "-c",
    'model_reasoning_effort="medium"',
)


def test_codex_argv_differs_only_by_plugin_switch() -> None:
    off = CodexInvocation(arm=Arm.OFF, inside_disposable_container=True).argv()
    on = CodexInvocation(arm=Arm.ON, inside_disposable_container=True).argv()

    assert off == (*EXPECTED_COMMON, "--disable", "plugins", "-C", "/workspace", "-")
    assert on == (*EXPECTED_COMMON, "--enable", "plugins", "-C", "/workspace", "-")
    assert "--ignore-user-config" not in off
    differences = [(a, b) for a, b in zip(off, on, strict=True) if a != b]
    assert differences == [("--disable", "--enable")]


def test_dangerous_codex_invocation_is_rejected_outside_container() -> None:
    with pytest.raises(UnsafeCodexInvocation):
        CodexInvocation(arm=Arm.OFF, inside_disposable_container=False).argv()


class FakeRunner:
    def __init__(self, result: CommandResult | BaseException) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        self.calls.append({"argv": tuple(argv), **kwargs})
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def command_result(stdout: str, *, returncode: int = 0, stderr: str = "") -> CommandResult:
    return CommandResult(("codex",), "/workspace", returncode, stdout, stderr)


def test_codex_runner_writes_exact_jsonl_and_summary_artifacts(tmp_path: Path) -> None:
    raw = (
        b'{"type":"thread.started","thread_id":"fake"}\n'
        b'{"type":"agent_message","message":"first"}\n'
        b'{"type":"agent_message","message":"last"}\n'
        b'{"type":"turn.completed","usage":{"input_tokens":7,"output_tokens":3}}\n'
    )
    runner = FakeRunner(command_result(raw.decode()))
    store = ArtifactStore(tmp_path / "result")

    outcome = CodexRunner(runner).run(
        CodexInvocation(Arm.ON, inside_disposable_container=True),
        prompt=b"exact prompt",
        cwd=tmp_path,
        store=store,
    )

    assert runner.calls[0]["input_bytes"] == b"exact prompt"
    assert (store.root / "codex/events.jsonl").read_bytes() == raw
    assert (store.root / "codex/stderr.txt").read_bytes() == b""
    assert (store.root / "codex/last-message.txt").read_text() == "last"
    assert json.loads((store.root / "codex/usage.json").read_text()) == {
        "input_tokens": 7,
        "output_tokens": 3,
    }
    assert outcome.last_message == "last"
    assert outcome.usage == {"input_tokens": 7, "output_tokens": 3}


def test_codex_runner_reports_missing_usage_as_na(tmp_path: Path) -> None:
    runner = FakeRunner(command_result('{"type":"agent_message","message":"done"}\n{"type":"turn.completed"}\n'))

    outcome = CodexRunner(runner).run(
        CodexInvocation(Arm.OFF, inside_disposable_container=True),
        prompt=b"prompt",
        cwd=tmp_path,
        store=ArtifactStore(tmp_path / "result"),
    )

    assert outcome.usage is None
    assert json.loads((tmp_path / "result/codex/usage.json").read_text()) == {"status": "N/A"}


@pytest.mark.parametrize(
    ("result", "match"),
    [
        (command_result('{"type":"agent_message"}\nnot-json\n'), "malformed"),
        (
            CommandFailed("failed", command_result("", returncode=19, stderr="failed")),
            "failed",
        ),
        (
            CommandTimedOut("timed out", command_result("", returncode=124)),
            "timed out",
        ),
    ],
)
def test_codex_failures_are_typed_infrastructure_outcomes(
    tmp_path: Path,
    result: CommandResult | BaseException,
    match: str,
) -> None:
    with pytest.raises(CodexInfrastructureError, match=match):
        CodexRunner(FakeRunner(result)).run(
            CodexInvocation(Arm.OFF, inside_disposable_container=True),
            prompt=b"prompt",
            cwd=tmp_path,
            store=ArtifactStore(tmp_path / "result"),
        )


def evidence(**changes: object) -> TreatmentEvidence:
    values: dict[str, object] = {
        "plugin_installed": True,
        "plugin_id": "powercontext@powercontext",
        "plugin_version": "0.1.0",
        "plugin_checkout_sha": "a" * 40,
        "server_ready": True,
        "prompt_sources": 1,
        "mcp_requests": 1,
        "scope_id": "eval:run-1:on",
    }
    values.update(changes)
    return TreatmentEvidence(**values)  # type: ignore[arg-type]


def test_treatment_evidence_accepts_valid_on_and_off() -> None:
    expected = {"expected_plugin_version": "0.1.0", "expected_checkout_sha": "a" * 40}
    assert validate_treatment(Arm.ON, "run-1", evidence(), **expected) is None
    assert (
        validate_treatment(
            Arm.OFF,
            "run-1",
            evidence(prompt_sources=0, mcp_requests=0, scope_id="eval:run-1:off"),
            **expected,
        )
        is None
    )


@pytest.mark.parametrize(
    ("arm", "changes"),
    [
        (Arm.ON, {"plugin_installed": False}),
        (Arm.ON, {"plugin_id": "other@market"}),
        (Arm.ON, {"plugin_version": "9.9.9"}),
        (Arm.ON, {"plugin_checkout_sha": "b" * 40}),
        (Arm.ON, {"server_ready": False}),
        (Arm.ON, {"prompt_sources": 0}),
        (Arm.ON, {"scope_id": "eval:other:on"}),
        (Arm.OFF, {"prompt_sources": 1, "scope_id": "eval:run-1:off"}),
        (Arm.OFF, {"mcp_requests": 1, "prompt_sources": 0, "scope_id": "eval:run-1:off"}),
    ],
)
def test_treatment_evidence_rejects_mismatch(arm: Arm, changes: dict[str, object]) -> None:
    with pytest.raises(InvalidTreatment):
        validate_treatment(
            arm,
            "run-1",
            evidence(**changes),
            expected_plugin_version="0.1.0",
            expected_checkout_sha="a" * 40,
        )


def make_paths(tmp_path: Path) -> ArmPaths:
    source = tmp_path / "source"
    auth = tmp_path / "outside-results" / "auth.json"
    workspace = tmp_path / "ephemeral" / "workspace"
    runtime = tmp_path / "ephemeral" / "runtime"
    codex_home = runtime / "codex-home"
    pc_home = runtime / "pc-home"
    for path in (source, auth.parent, workspace, codex_home, pc_home):
        path.mkdir(parents=True, exist_ok=True)
    auth.write_text('{"token":"fixture-secret"}')
    os.chmod(auth, 0o600)
    return ArmPaths(source, auth, workspace, runtime, codex_home, pc_home, tmp_path / "results")


class TranscriptDocker:
    def __init__(self, *, fail_at: str | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.fail_at = fail_at

    def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
        del kwargs
        self.commands.append(argv)
        if self.fail_at and self.fail_at in argv:
            raise CommandFailed("injected", command_result("", returncode=70))
        if argv[-3:] == ("network", "inspect", "powercontext-eval-run-1"):
            return command_result('[{"IPAM":{"Config":[{"Gateway":"172.29.0.1"}]}}]')
        if "plugin" in argv and "list" in argv:
            return command_result(
                json.dumps(
                    {
                        "installed": [
                            {
                                "pluginId": "powercontext@powercontext",
                                "version": "0.1.0",
                                "installed": True,
                                "enabled": True,
                            }
                        ],
                        "available": [],
                    }
                )
            )
        if any(part.endswith("/codex") for part in argv) and "--version" in argv:
            return command_result("codex-cli 0.145.0\n")
        if "evidence" in argv:
            scope = argv[argv.index("evidence") - 1]
            return command_result(json.dumps({"prompt_sources": 0 if scope.endswith(":off") else 1}))
        if any(part.endswith("/codex") or part == "codex" for part in argv) and "exec" in argv:
            return command_result(
                '{"type":"agent_message","message":"done"}\n'
                '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
            )
        return command_result("")


class FakeRelay:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def start(self, gateway: str, upstream: ProxyRelayConfig) -> str:
        self.events.append(("start", gateway))
        assert upstream.url == "http://127.0.0.1:7890"
        return f"http://{gateway}:17890"

    def stop(self) -> None:
        self.events.append(("stop", "exact"))


def sut_config(tmp_path: Path) -> SutConfig:
    return SutConfig(
        run_id="run-1",
        task_image="jefzda/sweap-images:fixture",
        codex_binary=tmp_path / "codex",
        uv_binary=tmp_path / "uv",
        source_checkout=tmp_path / "source",
        plugin_checkout_sha="a" * 40,
        proxy=ProxyRelayConfig("http://127.0.0.1:7890"),
        limits=ContainerLimits(cpus="2", memory="4g", pids=256),
    )


def test_sut_transcript_has_hardening_mount_allowlist_shared_network_and_scope(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    docker = TranscriptDocker()
    relay = FakeRelay()
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    DockerSut(docker, relay_factory=lambda: relay).run_arm(
        config, Arm.ON, paths, b"prompt", ArtifactStore(paths.result_root)
    )

    transcript = docker.commands
    run = next(command for command in transcript if command[:3] == ("docker", "run", "-d"))
    joined = " ".join(run)
    assert "--read-only" in run
    assert ("--cap-drop", "ALL") == run[run.index("--cap-drop") : run.index("--cap-drop") + 2]
    assert "no-new-privileges" in joined
    assert "--network powercontext-eval-run-1" in joined
    assert "--user 2950:100" in joined
    assert "--cpus 2" in joined and "--memory 4g" in joined and "--pids-limit 256" in joined
    assert "/var/run/docker.sock" not in joined
    assert str(Path.home()) not in joined
    assert "POWERCONTEXT_CODEX_SCOPE_ID=eval:run-1:on" in joined
    mounts = [run[index + 1] for index, value in enumerate(run) if value in {"--mount", "-v"}]
    assert all(
        any(allowed in mount for allowed in ("/workspace", "/runtime", "/source", "/tools/codex", "/tools/uv", "/auth"))
        for mount in mounts
    )
    assert transcript[-2][:3] == ("docker", "rm", "-f")
    assert transcript[-1][:3] == ("docker", "network", "rm")
    assert relay.events == [("start", "172.29.0.1"), ("stop", "exact")]
    assert any(command[-5:] == ("plugin", "marketplace", "add", "/source", "--json") for command in transcript)
    assert any(command[-4:] == ("plugin", "add", "powercontext@powercontext", "--json") for command in transcript)
    assert ("docker", "exec", "powercontext-eval-run-1-on", "/tools/codex", "plugin", "list", "--json") in transcript
    assert ("docker", "exec", "powercontext-eval-run-1-on", "/tools/codex", "--version") in transcript
    assert json.loads((paths.result_root / "codex/provenance.json").read_text()) == {
        "actual_version": "0.145.0",
        "expected_version": "0.145.0",
    }
    evidence_command = next(command for command in transcript if "evidence" in command)
    assert "eval:run-1:on" in evidence_command
    assert any("pc_sources" in part for part in evidence_command)
    chown_index = next(index for index, command in enumerate(transcript) if "/bin/chown" in command)
    prewarm_index = next(
        index
        for index, command in enumerate(transcript)
        if command[-6:] == ("sync", "--frozen", "--project", "/source", "--extra", "server")
    )
    chown = transcript[chown_index]
    assert chown_index < prewarm_index
    assert "--network" in chown and chown[chown.index("--network") + 1] == "none"
    assert ("--cap-drop", "ALL", "--cap-add", "CHOWN") == chown[
        chown.index("--cap-drop") : chown.index("--cap-drop") + 4
    ]
    assert chown[-4:] == ("--recursive", "2950:100", "/workspace", "/runtime")


def test_pair_reuses_one_relay_and_network_and_runs_off_then_on(tmp_path: Path) -> None:
    off_paths = make_paths(tmp_path / "off")
    on_paths = make_paths(tmp_path / "on")
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")
    docker = TranscriptDocker()
    relay = FakeRelay()

    outcomes = DockerSut(docker, relay_factory=lambda: relay).run_pair(
        config,
        paths={Arm.OFF: off_paths, Arm.ON: on_paths},
        prompts={Arm.OFF: b"same", Arm.ON: b"same"},
        stores={
            Arm.OFF: ArtifactStore(off_paths.result_root),
            Arm.ON: ArtifactStore(on_paths.result_root),
        },
    )

    assert set(outcomes) == {Arm.OFF, Arm.ON}
    assert sum(command[:3] == ("docker", "network", "create") for command in docker.commands) == 1
    assert sum(command[:3] == ("docker", "network", "rm") for command in docker.commands) == 1
    assert relay.events == [("start", "172.29.0.1"), ("stop", "exact")]
    task_runs = [command for command in docker.commands if command[:3] == ("docker", "run", "-d")]
    assert [
        next(value for value in command if value.startswith("POWERCONTEXT_CODEX_SCOPE_ID=")) for command in task_runs
    ] == [
        "POWERCONTEXT_CODEX_SCOPE_ID=eval:run-1:off",
        "POWERCONTEXT_CODEX_SCOPE_ID=eval:run-1:on",
    ]
    proxy_values = [next(value for value in command if value.startswith("HTTPS_PROXY=")) for command in task_runs]
    assert proxy_values == ["HTTPS_PROXY=http://172.29.0.1:17890"] * 2


@pytest.mark.parametrize("fail_at", ["run", "exec", "evidence"])
def test_sut_faults_clean_up_only_exact_owned_resources(tmp_path: Path, fail_at: str) -> None:
    paths = make_paths(tmp_path)
    docker = TranscriptDocker(fail_at=fail_at)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    with pytest.raises(CommandFailed):
        DockerSut(docker, relay_factory=FakeRelay).run_arm(
            config, Arm.ON, paths, b"prompt", ArtifactStore(paths.result_root)
        )

    if fail_at != "run":
        assert ("docker", "rm", "-f", "powercontext-eval-run-1-on") in docker.commands
    assert ("docker", "network", "rm", "powercontext-eval-run-1") in docker.commands
    assert not any("unowned" in part for command in docker.commands for part in command)


@pytest.mark.parametrize(
    "upstream",
    [
        "http://10.0.0.1:7890",
        "http://user:password@127.0.0.1:7890",
        "http://127.0.0.1:7890/path",
    ],
)
def test_relay_rejects_unsafe_upstream(upstream: str) -> None:
    with pytest.raises(UnsafeSutConfiguration):
        ProxyRelayConfig(upstream)


@pytest.mark.parametrize("run_id", ["-option", "has space", "a/b", "UPPER"])
def test_sut_rejects_unsafe_run_names(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(UnsafeSutConfiguration):
        SutConfig(
            run_id=run_id,
            task_image="image:tag",
            codex_binary=tmp_path / "codex",
            uv_binary=tmp_path / "uv",
            source_checkout=tmp_path / "source",
            plugin_checkout_sha="a" * 40,
            proxy=ProxyRelayConfig("http://127.0.0.1:7890"),
        )


def test_gateway_inspect_malformed_is_rejected(tmp_path: Path) -> None:
    class MalformedGatewayDocker(TranscriptDocker):
        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            del argv, kwargs
            return command_result("{}")

    docker = MalformedGatewayDocker()
    with pytest.raises(UnsafeSutConfiguration):
        DockerSut(docker).network_gateway("powercontext-eval-run-1", tmp_path)


def test_socat_relay_binds_only_gateway_and_stops_exact_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class FakeProcess:
        pid = 4321
        stderr = io.BytesIO()

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            events.append(("wait", timeout))
            return 0

    def popen(argv: tuple[str, ...], **kwargs: object) -> FakeProcess:
        events.append(("popen", argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr("powercontext_eval.powercontext_sut._reserve_port", lambda _address: 17890)
    monkeypatch.setattr("powercontext_eval.powercontext_sut.subprocess.Popen", popen)
    monkeypatch.setattr(
        "powercontext_eval.powercontext_sut.socket.create_connection",
        lambda address, timeout: events.append(("ready", address, timeout)) or io.BytesIO(),
    )
    monkeypatch.setattr(
        "powercontext_eval.powercontext_sut.os.killpg",
        lambda pid, sig: events.append(("killpg", pid, sig)),
    )
    relay = SocatProxyRelay()

    url = relay.start("172.29.0.1", ProxyRelayConfig("http://127.0.0.1:7890"))
    relay.stop()

    assert url == "http://172.29.0.1:17890"
    popen_event = events[0]
    assert isinstance(popen_event, tuple)
    assert popen_event[1] == (
        "socat",
        "TCP-LISTEN:17890,bind=172.29.0.1,fork,reuseaddr",
        "TCP:127.0.0.1:7890",
    )
    assert ("killpg", 4321, 15) in events


def test_socat_readiness_timeout_cleans_up_exact_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, int]] = []

    class FakeProcess:
        pid = 8765
        stderr = io.BytesIO()

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    monkeypatch.setattr("powercontext_eval.powercontext_sut._reserve_port", lambda _address: 17891)
    monkeypatch.setattr("powercontext_eval.powercontext_sut.subprocess.Popen", lambda *_args, **_kwargs: FakeProcess())
    monkeypatch.setattr(
        "powercontext_eval.powercontext_sut.os.killpg",
        lambda pid, sig: killed.append((pid, int(sig))),
    )

    with pytest.raises(UnsafeSutConfiguration, match="timed out"):
        SocatProxyRelay(readiness_timeout=0).start(
            "172.29.0.1",
            ProxyRelayConfig("http://127.0.0.1:7890"),
        )

    assert killed == [(8765, 15)]


def test_auth_is_copied_minimally_with_mode_0600_and_homes_are_not_results(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    destination = paths.copy_auth()

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert destination.read_bytes() == paths.auth_source.read_bytes()
    assert not paths.codex_home.is_relative_to(paths.result_root)
    assert not paths.pc_home.is_relative_to(paths.result_root)


def test_fake_codex_fixture_is_executable_and_offline(tmp_path: Path) -> None:
    del tmp_path
    fake = Path(__file__).parent / "fixtures/fake_codex.py"
    os.chmod(fake, 0o755)
    result = subprocess.run(
        [sys.executable, os.fspath(fake)],
        input=b"hello",
        capture_output=True,
        check=True,
    )
    assert b"turn.completed" in result.stdout
