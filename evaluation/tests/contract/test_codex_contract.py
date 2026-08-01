from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from base64 import b64encode, urlsafe_b64encode
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import quote, quote_plus
from urllib.request import urlopen

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
    LOOPBACK_NO_PROXY,
    ArmPaths,
    ContainerLimits,
    DockerSut,
    InvalidTreatment,
    ProxyRelayConfig,
    SocatProxyRelay,
    SutConfig,
    TreatmentEvidence,
    UnsafeSutConfiguration,
    auth_secret_variants,
    loopback_proxy_environment,
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


def test_timestamp_recorder_wraps_but_does_not_change_the_codex_command() -> None:
    base = CodexInvocation(arm=Arm.ON, inside_disposable_container=True).argv()
    wrapped = CodexInvocation(
        arm=Arm.ON,
        inside_disposable_container=True,
        recorder_python="/runtime/pc-env/bin/python",
        recorder_script="/source/evaluation/scripts/record_codex_jsonl.py",
        recorder_sidecar="/runtime/pc-home/codex-observed.jsonl",
    ).argv()

    assert wrapped[:5] == (
        "/runtime/pc-env/bin/python",
        "/source/evaluation/scripts/record_codex_jsonl.py",
        "--sidecar",
        "/runtime/pc-home/codex-observed.jsonl",
        "--",
    )
    assert wrapped[5:] == base


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


def test_codex_runner_uses_bounded_stream_sink_and_keeps_nonzero_evidence(tmp_path: Path) -> None:
    raw = b'{"type":"agent_message","message":"done"}\n{"type":"turn.completed"}\n'

    class StreamingRunner:
        def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
            sink = cast(BinaryIO, kwargs["stdout_sink"])
            sink.write(raw)
            return CommandResult(tuple(argv), str(tmp_path), 0, "", "warning")

    store = ArtifactStore(tmp_path / "result")
    outcome = CodexRunner(StreamingRunner()).run(
        CodexInvocation(Arm.ON, inside_disposable_container=True),
        prompt=b"prompt",
        cwd=tmp_path,
        store=store,
    )

    assert outcome.last_message == "done"
    assert (store.root / "codex/events.jsonl").read_bytes() == raw
    assert (store.root / "codex/stderr.txt").read_text() == "warning"


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
    for path in (source, auth.parent):
        path.mkdir(parents=True, exist_ok=True)
    auth.write_text('{"token":"fixture-secret"}')
    os.chmod(auth, 0o600)
    return ArmPaths(source, auth, workspace, runtime, codex_home, pc_home, tmp_path / "results")


class TranscriptDocker:
    def __init__(self, *, fail_at: str | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.fail_at = fail_at

    def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
        cwd = Path(cast(str | Path, kwargs.get("cwd", "/workspace")))
        self.commands.append(argv)
        if self.fail_at and self.fail_at in argv:
            raise CommandFailed("injected", command_result("", returncode=70))
        if argv[:2] == ("git", "rev-parse"):
            return command_result("a" * 40 + "\n")
        if argv[:2] == ("git", "status"):
            return command_result("")
        if argv[-3:-1] == ("network", "inspect"):
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
            result = command_result(
                '{"type":"agent_message","message":"done"}\n'
                '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
            )
            if "/evaluation/record_codex_jsonl.py" in argv:
                pc_home = cwd.parent / "runtime" / "pc-home"
                pc_home.mkdir(parents=True, exist_ok=True)
                events = [json.loads(line) for line in result.stdout.splitlines()]
                pc_home.joinpath("codex-observed.jsonl").write_text(
                    "".join(
                        json.dumps(
                            {
                                "sequence": sequence,
                                "observed_at": f"2026-07-29T08:10:11.{sequence:06d}Z",
                                "event": event,
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                        for sequence, event in enumerate(events, start=1)
                    )
                )
            return result
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
    manifest = tmp_path / "source/integrations/codex/plugins/powercontext/.codex-plugin/plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": "powercontext", "version": "0.1.0"}))
    lock = tmp_path / "source/integrations/codex/plugins/powercontext/uv.lock"
    lock.write_text("version = 1\n")
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


def test_workspace_initialization_recovers_from_docker_cp_rejecting_an_escaping_symlink(
    tmp_path: Path,
) -> None:
    class InvalidSymlinkCopyDocker(TranscriptDocker):
        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if argv[:2] == ("docker", "cp"):
                self.commands.append(argv)
                cwd = os.fspath(kwargs["cwd"])
                result = CommandResult(
                    argv,
                    cwd,
                    1,
                    "",
                    'invalid symlink "/app/node_modules/example" -> "../../../.cache/example"',
                )
                raise CommandFailed("injected invalid symlink", result)
            return super().run(argv, **kwargs)

    paths = make_paths(tmp_path)
    paths.prepare()
    docker = InvalidSymlinkCopyDocker()

    DockerSut(docker)._initialize_workspace(sut_config(tmp_path), Arm.OFF, paths)

    fallback = next(
        command
        for command in docker.commands
        if command[:2] == ("docker", "run") and command[-2:] == ("/app/.", "/workspace")
    )
    assert "--network" in fallback
    assert fallback[fallback.index("--network") + 1] == "none"
    assert ("--cap-drop", "ALL") == fallback[fallback.index("--cap-drop") : fallback.index("--cap-drop") + 2]
    assert "no-new-privileges" in " ".join(fallback)
    assert fallback[fallback.index("--user") + 1] == "2950:100"
    assert fallback[fallback.index("--entrypoint") + 1] == "/bin/cp"
    assert fallback[-4:] == ("--archive", "--no-preserve=ownership", "/app/.", "/workspace")


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
    assert (
        "docker",
        "cp",
        "powercontext-eval-run-1-on-init:/app/.",
        str(paths.workspace),
    ) in transcript
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
        any(
            allowed in mount
            for allowed in (
                "/workspace",
                "/runtime",
                "/source",
                "/evaluation",
                "/tools/codex-dir",
                "/tools/uv-dir",
                "/auth",
            )
        )
        for mount in mounts
    )
    assert transcript[-2][:3] == ("docker", "rm", "-f")
    assert transcript[-1][:3] == ("docker", "network", "rm")
    assert relay.events == [("start", "172.29.0.1"), ("stop", "exact")]
    assert any(command[-5:] == ("plugin", "marketplace", "add", "/source", "--json") for command in transcript)
    assert any(command[-4:] == ("plugin", "add", "powercontext@powercontext", "--json") for command in transcript)
    assert (
        "docker",
        "exec",
        "powercontext-eval-run-1-on",
        "/tools/codex-dir/codex",
        "plugin",
        "list",
        "--json",
    ) in transcript
    assert ("docker", "exec", "powercontext-eval-run-1-on", "/tools/codex-dir/codex", "--version") in transcript
    assert json.loads((paths.result_root / "codex/provenance.json").read_text()) == {
        "actual_version": "0.145.0",
        "expected_version": "0.145.0",
    }
    source_provenance = json.loads((paths.result_root / "powercontext/provenance.json").read_text())
    assert source_provenance["checkout_sha"] == "a" * 40
    assert source_provenance["plugin_version"] == "0.1.0"
    assert len(source_provenance["plugin_manifest_sha256"]) == 64
    evidence_command = next(command for command in transcript if "evidence" in command)
    assert "eval:run-1:on" in evidence_command
    assert any("pc_sources" in part for part in evidence_command)
    prewarm_index = next(
        index
        for index, command in enumerate(transcript)
        if command[-8:] == ("sync", "--frozen", "--project", "/source", "--extra", "server", "--extra", "cli")
    )
    chown_entries = [(index, command) for index, command in enumerate(transcript) if "/bin/chown" in command]
    if chown_entries:
        chown_index, chown = chown_entries[0]
        assert chown_index < prewarm_index
        assert "--network" in chown and chown[chown.index("--network") + 1] == "none"
        assert ("--cap-drop", "ALL", "--cap-add", "CHOWN") == chown[
            chown.index("--cap-drop") : chown.index("--cap-drop") + 4
        ]
        assert chown[-4:] == ("--recursive", "2950:100", "/workspace", "/runtime")
    else:
        assert all((path.stat().st_uid, path.stat().st_gid) == (2950, 100) for path in (paths.workspace, paths.runtime))


def test_distinct_run_ids_derive_distinct_runtime_network_and_scope(tmp_path: Path) -> None:
    runtimes: list[Path] = []
    networks: list[str] = []
    scopes: list[str] = []

    for run_id in ("parallel-run-a", "parallel-run-b"):
        root = tmp_path / run_id
        paths = make_paths(root)
        config = replace(sut_config(root), run_id=run_id)
        config.codex_binary.write_text("binary")
        config.uv_binary.write_text("binary")
        docker = TranscriptDocker()
        DockerSut(docker, relay_factory=FakeRelay).run_arm(
            config,
            Arm.ON,
            paths,
            b"prompt",
            ArtifactStore(paths.result_root),
        )
        run = next(command for command in docker.commands if command[:3] == ("docker", "run", "-d"))
        evidence_command = next(command for command in docker.commands if "evidence" in command)
        runtimes.append(paths.runtime)
        networks.append(run[run.index("--network") + 1])
        scopes.append(evidence_command[evidence_command.index("evidence") - 1])

    assert runtimes[0] != runtimes[1]
    assert networks == ["powercontext-eval-parallel-run-a", "powercontext-eval-parallel-run-b"]
    assert scopes == ["eval:parallel-run-a:on", "eval:parallel-run-b:on"]


def test_sut_uses_timestamp_recorder_and_retains_private_context_traces(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class TraceDocker(TranscriptDocker):
        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            result = super().run(argv, **kwargs)
            if "/evaluation/record_codex_jsonl.py" in argv:
                paths.pc_home.joinpath("codex-observed.jsonl").write_text(
                    '{"sequence":1,"observed_at":"2026-07-29T08:10:11.100000Z",'
                    '"event":{"type":"agent_message","message":"done"}}\n'
                    '{"sequence":2,"observed_at":"2026-07-29T08:10:11.200000Z",'
                    '"event":{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}}\n'
                )
                paths.pc_home.joinpath("evaluation-injections.jsonl").write_text(
                    '{"event_type":"powercontext_injection",'
                    '"observed_at":"2026-07-29T08:10:11.150000Z",'
                    '"query":"prompt","injected_text":"PowerContext recalled one fact.",'
                    '"hits":[{"text":"one fact"}],"scope_id":"eval:run-1:on"}\n'
                )
            return result

    docker = TraceDocker()

    DockerSut(docker, relay_factory=FakeRelay).run_arm(
        config,
        Arm.ON,
        paths,
        b"prompt",
        ArtifactStore(paths.result_root),
    )

    codex_command = next(command for command in docker.commands if "/evaluation/record_codex_jsonl.py" in command)
    assert "/runtime/pc-env/bin/python" in codex_command
    assert "/runtime/pc-home/codex-observed.jsonl" in codex_command
    assert "POWERCONTEXT_EVAL_TRACE_PATH=/runtime/pc-home/evaluation-injections.jsonl" in codex_command
    assert (paths.result_root / "context/codex-observed.jsonl").read_text().startswith('{"sequence":1')
    assert (
        (paths.result_root / "context/powercontext-injections.jsonl")
        .read_text()
        .startswith('{"event_type":"powercontext_injection"')
    )


def test_pair_reuses_one_relay_and_network_and_runs_off_then_on(tmp_path: Path) -> None:
    off_paths = make_paths(tmp_path / "off")
    on_paths = make_paths(tmp_path / "on")
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")
    docker = TranscriptDocker()
    relay = FakeRelay()
    started_arms: list[Arm] = []

    outcomes = DockerSut(docker, relay_factory=lambda: relay).run_pair(
        config,
        paths={Arm.OFF: off_paths, Arm.ON: on_paths},
        prompts={Arm.OFF: b"same", Arm.ON: b"same"},
        stores={
            Arm.OFF: ArtifactStore(off_paths.result_root),
            Arm.ON: ArtifactStore(on_paths.result_root),
        },
        before_arm=started_arms.append,
    )

    assert set(outcomes) == {Arm.OFF, Arm.ON}
    assert started_arms == [Arm.OFF, Arm.ON]
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


def test_all_container_phases_receive_identical_loopback_bypass_environment(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")
    docker = TranscriptDocker()

    DockerSut(docker, relay_factory=FakeRelay).run_arm(
        config, Arm.ON, paths, b"prompt", ArtifactStore(paths.result_root)
    )

    container_commands = [
        command
        for command in docker.commands
        if command[:2] in {("docker", "run"), ("docker", "exec")} and "--network" in command
    ]
    assert container_commands
    for command in container_commands:
        assert f"NO_PROXY={LOOPBACK_NO_PROXY}" in command
        assert f"no_proxy={LOOPBACK_NO_PROXY}" in command


def test_urllib_loopback_bypasses_an_unreachable_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_path
    import http.server
    import threading

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = loopback_proxy_environment("http://127.0.0.1:1")
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}", timeout=2) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        thread.join()


def test_auth_secrets_include_nested_scalars_and_supported_derivations(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": [{"access": "a/b +?秘密"}], "account": {"id": 12345}}))

    variants = auth_secret_variants(auth)

    raw = "a/b +?秘密"
    encoded = raw.encode()
    assert {
        raw,
        quote(raw, safe=""),
        quote_plus(raw, safe=""),
        b64encode(encoded).decode(),
        urlsafe_b64encode(encoded).decode(),
        encoded.hex(),
        "12345",
    } <= set(variants)


def test_fake_codex_echo_of_auth_secrets_or_encodings_is_never_published(tmp_path: Path) -> None:
    raw = "fixture/super secret"
    variants = auth_secret_variants(_write_json(tmp_path / "auth.json", {"nested": {"token": raw}}))
    leaked = quote_plus(raw, safe="")

    class LeakingStreamRunner:
        def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
            del argv
            sink = cast(BinaryIO, kwargs["stdout_sink"])
            sink.write(f'{{"type":"agent_message","message":"{leaked}"}}\n'.encode())
            return command_result("")

    store = ArtifactStore(tmp_path / "result")

    with pytest.raises(CodexInfrastructureError, match="secret"):
        CodexRunner(LeakingStreamRunner()).run(
            CodexInvocation(Arm.ON, inside_disposable_container=True),
            prompt=b"prompt",
            cwd=tmp_path,
            store=store,
            secrets=variants,
        )

    assert list(store.root.rglob("*")) == []


def test_server_log_echo_of_auth_encoding_is_rejected_before_publication(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    secret = "server/log secret"
    paths.auth_source.write_text(json.dumps({"nested": {"token": secret}}))
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class LeakingLogsDocker(TranscriptDocker):
        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if argv[:2] == ("docker", "logs"):
                self.commands.append(argv)
                return command_result(quote_plus(secret, safe=""))
            return super().run(argv, **kwargs)

    with pytest.raises(CodexInfrastructureError, match="secret"):
        DockerSut(LeakingLogsDocker(), relay_factory=FakeRelay).run_arm(
            config, Arm.ON, paths, b"prompt", ArtifactStore(paths.result_root)
        )
    assert not (paths.result_root / "powercontext/server.log").exists()


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize("target", ["workspace", "runtime"])
@pytest.mark.parametrize("kind", ["stale", "symlink"])
def test_arm_paths_reject_stale_or_symlink_roots(tmp_path: Path, target: str, kind: str) -> None:
    paths = make_paths(tmp_path)
    selected = getattr(paths, target)
    if kind == "stale":
        selected.mkdir(parents=True)
        (selected / "old").write_text("stale")
    else:
        destination = tmp_path / f"{target}-elsewhere"
        destination.mkdir()
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.symlink_to(destination, target_is_directory=True)

    with pytest.raises(UnsafeSutConfiguration, match="fresh"):
        paths.prepare()


def test_pair_rejects_shared_or_nonempty_arm_roots(tmp_path: Path) -> None:
    off = make_paths(tmp_path / "off")
    on = make_paths(tmp_path / "on")
    object.__setattr__(on, "workspace", off.workspace)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    with pytest.raises(UnsafeSutConfiguration):
        DockerSut(TranscriptDocker(), relay_factory=FakeRelay).run_pair(
            config,
            paths={Arm.OFF: off, Arm.ON: on},
            prompts={Arm.OFF: b"x", Arm.ON: b"x"},
            stores={Arm.OFF: ArtifactStore(off.result_root), Arm.ON: ArtifactStore(on.result_root)},
        )


def test_source_head_and_manifest_are_verified_before_any_docker_command(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class WrongHeadDocker(TranscriptDocker):
        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if argv[:2] == ("git", "rev-parse"):
                self.commands.append(argv)
                return command_result("b" * 40 + "\n")
            return super().run(argv, **kwargs)

    docker = WrongHeadDocker()
    with pytest.raises(InvalidTreatment, match="HEAD"):
        DockerSut(docker, relay_factory=FakeRelay).run_arm(
            config, Arm.ON, paths, b"prompt", ArtifactStore(paths.result_root)
        )
    assert docker.commands == [("git", "rev-parse", "--verify", "HEAD^{commit}")]


def test_manifest_version_mismatch_is_rejected_before_docker(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    manifest = config.source_checkout / "integrations/codex/plugins/powercontext/.codex-plugin/plugin.json"
    manifest.write_text(json.dumps({"name": "powercontext", "version": "9.9.9"}))
    docker = TranscriptDocker()

    with pytest.raises(InvalidTreatment, match="manifest"):
        DockerSut(docker, relay_factory=FakeRelay).run_arm(
            config, Arm.ON, paths, b"prompt", ArtifactStore(paths.result_root)
        )
    assert not any(command[:2] == ("docker", "network") for command in docker.commands)


@pytest.mark.parametrize("dirty_output", [" M src/powercontext/__init__.py\n", "?? untracked-secret\n"])
def test_dirty_source_is_rejected_before_any_docker_resource(tmp_path: Path, dirty_output: str) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)

    class DirtySourceDocker(TranscriptDocker):
        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if argv[:2] == ("git", "status"):
                self.commands.append(argv)
                return command_result(dirty_output)
            return super().run(argv, **kwargs)

    docker = DirtySourceDocker()
    with pytest.raises(InvalidTreatment, match="clean"):
        DockerSut(docker, relay_factory=FakeRelay).run_arm(
            config, Arm.ON, paths, b"prompt", ArtifactStore(paths.result_root)
        )
    assert not any(command[:2] == ("docker", "network") for command in docker.commands)


def test_plugin_locked_environment_is_prewarmed_and_injected_into_hook_path(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")
    docker = TranscriptDocker()

    DockerSut(docker, relay_factory=FakeRelay).run_arm(
        config, Arm.ON, paths, b"prompt", ArtifactStore(paths.result_root)
    )

    plugin_sync = next(
        command
        for command in docker.commands
        if "/source/integrations/codex/plugins/powercontext" in command
        and "UV_PROJECT_ENVIRONMENT=/runtime/plugin-env" in command
    )
    assert plugin_sync[-5:] == (
        "sync",
        "--frozen",
        "--project",
        "/source/integrations/codex/plugins/powercontext",
        "--no-install-project",
    )
    task = next(command for command in docker.commands if command[:3] == ("docker", "run", "-d"))
    assert "UV_PROJECT_ENVIRONMENT=/runtime/plugin-env" in task
    assert "UV_CACHE_DIR=/runtime/uv-cache" in task
    assert "UV_OFFLINE=1" in task


def test_transient_plugin_install_failure_is_retried_after_partial_codex_config_write(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class InterruptedPluginInstallDocker(TranscriptDocker):
        interrupted = False

        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if not self.interrupted and argv[-4:] == ("plugin", "add", "powercontext@powercontext", "--json"):
                self.commands.append(argv)
                self.interrupted = True
                raise CommandFailed("injected partial plugin install", command_result("", returncode=70))
            return super().run(argv, **kwargs)

    docker = InterruptedPluginInstallDocker()

    DockerSut(docker, relay_factory=FakeRelay).run_arm(
        config,
        Arm.ON,
        paths,
        b"prompt",
        ArtifactStore(paths.result_root),
    )

    plugin_installs = [
        command
        for command in docker.commands
        if command[-4:] == ("plugin", "add", "powercontext@powercontext", "--json")
    ]
    assert len(plugin_installs) == 2


def test_transient_readiness_probe_timeout_is_retried(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class SlowFirstReadinessProbeDocker(TranscriptDocker):
        timed_out = False

        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if not self.timed_out and "doctor" in argv:
                self.commands.append(argv)
                self.timed_out = True
                raise CommandTimedOut("injected readiness timeout", command_result("", returncode=124))
            return super().run(argv, **kwargs)

    docker = SlowFirstReadinessProbeDocker()

    DockerSut(docker, relay_factory=FakeRelay).run_arm(
        config,
        Arm.ON,
        paths,
        b"prompt",
        ArtifactStore(paths.result_root),
    )

    readiness_probes = [command for command in docker.commands if "doctor" in command]
    assert len(readiness_probes) == 2


def test_managed_python_is_kept_in_the_writable_arm_runtime(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")
    docker = TranscriptDocker()

    DockerSut(docker, relay_factory=FakeRelay).run_arm(
        config, Arm.ON, paths, b"prompt", ArtifactStore(paths.result_root)
    )

    uv_consumers = [
        command for command in docker.commands if any(value.startswith("UV_PROJECT_ENVIRONMENT=") for value in command)
    ]
    assert len(uv_consumers) >= 4
    assert all("UV_PYTHON_INSTALL_DIR=/runtime/uv-python" in command for command in uv_consumers)


def test_network_cleanup_survives_relay_stop_failure(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    docker = TranscriptDocker()

    class BrokenStopRelay(FakeRelay):
        def stop(self) -> None:
            raise RuntimeError("stop failed")

    with pytest.raises(RuntimeError, match="stop failed"):
        DockerSut(docker, relay_factory=BrokenStopRelay).run_arm(
            config, Arm.ON, paths, b"prompt", ArtifactStore(paths.result_root)
        )
    assert ("docker", "network", "rm", "powercontext-eval-run-1") in docker.commands
