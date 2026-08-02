from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
from base64 import b64encode, urlsafe_b64encode
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast
from urllib.parse import quote, quote_plus
from urllib.request import urlopen

import pytest

from powercontext_eval.artifacts import ArtifactStore, SecretDetected
from powercontext_eval.codex import (
    CodexInfrastructureError,
    CodexInvocation,
    CodexRunner,
    UnsafeCodexInvocation,
)
from powercontext_eval.errors import CommandError
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
    run_codex_contract_smoke,
    validate_treatment,
)
from powercontext_eval.process import CommandFailed, CommandResult, CommandTimedOut, ProcessRunner
from powercontext_eval.tokensflow import TokensFlowInfrastructureError

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
    tokensflow_home = runtime / "tokensflow-home"
    for path in (source, auth.parent):
        path.mkdir(parents=True, exist_ok=True)
    auth.write_text('{"token":"fixture-secret"}')
    os.chmod(auth, 0o600)
    tokensflow_config = tokensflow_home / ".tokensflow"
    tokensflow_config.mkdir(parents=True)
    (tokensflow_config / "credentials.json").write_text('{"token":"fixture-tokensflow-secret"}')
    return ArmPaths(
        source=source,
        auth_source=auth,
        workspace=workspace,
        runtime=runtime,
        codex_home=codex_home,
        pc_home=pc_home,
        result_root=tmp_path / "results",
        tokensflow_home=tokensflow_home,
    )


class TranscriptDocker:
    def __init__(
        self,
        *,
        fail_at: str | None = None,
        host_identity: bytes = b"fixture-person\n",
        container_identity: bytes | None = None,
        host_tokensflow_version: bytes = b"tokensflow 1.0.16\n",
        container_tokensflow_version: bytes | None = None,
    ) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.fail_at = fail_at
        self.host_identity = host_identity
        self.container_identity = container_identity if container_identity is not None else host_identity
        self.host_tokensflow_version = host_tokensflow_version
        self.container_tokensflow_version = (
            container_tokensflow_version if container_tokensflow_version is not None else host_tokensflow_version
        )

    @staticmethod
    def _output(payload: bytes, kwargs: dict[str, object], *, returncode: int = 0) -> CommandResult:
        sink = kwargs.get("stdout_sink")
        if sink is not None:
            cast(BinaryIO, sink).write(payload)
            return command_result("", returncode=returncode)
        return command_result(payload.decode("utf-8"), returncode=returncode)

    def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
        cwd = Path(cast(str | Path, kwargs.get("cwd", "/workspace")))
        self.commands.append(argv)
        if self.fail_at and self.fail_at in argv:
            raise CommandFailed("injected", command_result("", returncode=70))
        if argv[:2] == ("git", "rev-parse"):
            return command_result("a" * 40 + "\n")
        if argv[:2] == ("git", "status"):
            return command_result("")
        if argv[-1:] == ("whoami",) and any(part.endswith("/tokensflow") for part in argv):
            identity = self.container_identity if argv[0] == "docker" else self.host_identity
            return self._output(identity, kwargs)
        if argv[-1:] == ("--version",) and any(part.endswith("/tokensflow") for part in argv):
            version = self.container_tokensflow_version if argv[0] == "docker" else self.host_tokensflow_version
            return self._output(version, kwargs)
        if _is_tokensflow(argv, "upload"):
            return self._output(b"uploaded=1 duplicates=0\n", kwargs)
        if _is_tokensflow(argv, "doctor"):
            return self._output(
                b"[FAIL] daemon: not running after graceful TERM\n"
                b"[PASS] queue: caught up (0 pending files) (within collection window)\n",
                kwargs,
                returncode=1,
            )
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
    tokensflow_binary = tmp_path / "tokensflow"
    tokensflow_binary.write_text("binary")
    tokensflow_binary.chmod(0o755)
    return SutConfig(
        run_id="run-1",
        task_image="jefzda/sweap-images:fixture",
        codex_binary=tmp_path / "codex",
        uv_binary=tmp_path / "uv",
        source_checkout=tmp_path / "source",
        plugin_checkout_sha="a" * 40,
        proxy=ProxyRelayConfig("http://127.0.0.1:7890"),
        limits=ContainerLimits(cpus="2", memory="4g", pids=256),
        tokensflow_binary=tokensflow_binary,
    )


def test_workspace_initialization_recovers_from_docker_cp_rejecting_an_escaping_symlink(
    tmp_path: Path,
) -> None:
    class InvalidSymlinkCopyDocker(TranscriptDocker):
        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if argv[:2] == ("docker", "cp"):
                self.commands.append(argv)
                cwd = os.fspath(cast(str | Path, kwargs["cwd"]))
                Path(argv[-1], "partial-copy").write_text("must be discarded")
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
    assert not paths.workspace.joinpath("partial-copy").exists()


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
                "/tools/tokensflow-dir",
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


def _is_codex_inference(command: tuple[str, ...]) -> bool:
    return any(
        index + 1 < len(command) and command[index + 1] == "exec"
        for index, part in enumerate(command)
        if part.endswith("/codex")
    )


def _is_tokensflow(command: tuple[str, ...], action: str) -> bool:
    return any("/tools/tokensflow-dir/tokensflow" in part for part in command) and any(
        action in part for part in command
    )


def test_tokensflow_identity_gate_mounts_dynamic_binary_and_starts_one_arm_daemon(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")
    raw_host_identity = b"fixture-person\r\n"
    raw_container_identity = b"fixture-person\n"
    raw_version = b"tokensflow 1.0.16\n"
    docker = TranscriptDocker(
        host_identity=raw_host_identity,
        container_identity=raw_container_identity,
        host_tokensflow_version=raw_version,
    )

    outcome = DockerSut(docker, relay_factory=FakeRelay).run_arm(
        config,
        Arm.ON,
        paths,
        b"prompt",
        ArtifactStore(paths.result_root),
    )

    task = next(command for command in docker.commands if command[:3] == ("docker", "run", "-d"))
    mounts = [task[index + 1] for index, value in enumerate(task) if value == "--mount"]
    assert f"type=bind,src={config.tokensflow_binary.resolve().parent},dst=/tools/tokensflow-dir,readonly" in mounts
    assert "HOME=/runtime/tokensflow-home" in task
    assert "CODEX_HOME=/runtime/codex-home" in task

    host_whoami_index = next(
        index
        for index, command in enumerate(docker.commands)
        if command == (os.fspath(config.tokensflow_binary), "whoami")
    )
    container_whoami_index = next(
        index
        for index, command in enumerate(docker.commands)
        if command[-2:] == ("/tools/tokensflow-dir/tokensflow", "whoami")
    )
    daemon_entries = [
        (index, command)
        for index, command in enumerate(docker.commands)
        if command[:3] == ("docker", "exec", "-d") and _is_tokensflow(command, "daemon")
    ]
    assert len(daemon_entries) == 1
    daemon_index, daemon = daemon_entries[0]
    codex_index = next(index for index, command in enumerate(docker.commands) if _is_codex_inference(command))
    assert host_whoami_index < container_whoami_index < daemon_index < codex_index
    assert "HOME=/runtime/tokensflow-home" in daemon
    assert "CODEX_HOME=/runtime/codex-home" in daemon
    assert "evaluation-daemon.pid" in " ".join(daemon)
    assert "evaluation-daemon.log" in " ".join(daemon)
    readiness = next(
        command
        for command in docker.commands
        if command[:3] == ("docker", "exec", "powercontext-eval-run-1-on")
        and any("evaluation-daemon.pid" in part for part in command)
    )
    readiness_script = readiness[-1]
    assert 'readlink "/proc/$pid/exe"' in readiness_script
    assert 'tr "\\000" "\\n" < "/proc/$pid/cmdline" | grep -Fqx daemon' in readiness_script
    assert "kill -0" not in readiness_script

    tokensflow = outcome.tokensflow
    assert tokensflow.host_version == "1.0.16"
    assert tokensflow.container_version == "1.0.16"
    assert tokensflow.identity_match is True
    assert tokensflow.identity_bytes == len(b"fixture-person")
    assert tokensflow.host_identity_sha256 == tokensflow.container_identity_sha256
    assert tokensflow.host_identity_sha256 == hashlib.sha256(b"fixture-person").hexdigest()
    assert tokensflow.daemon_started is True
    provenance = json.loads((paths.result_root / "tokensflow/provenance.json").read_text())
    assert provenance["host_version"] == "1.0.16"
    assert provenance["identity_bytes"] == len(b"fixture-person")
    assert provenance["host_identity_sha256"] == hashlib.sha256(b"fixture-person").hexdigest()
    retained = b"".join(path.read_bytes() for path in paths.result_root.rglob("*") if path.is_file())
    assert raw_host_identity not in retained
    assert raw_container_identity not in retained
    assert raw_version not in retained


def test_tokensflow_drain_obeys_zero_loss_order_and_accepts_duplicate_replay(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class DuplicateReplayDocker(TranscriptDocker):
        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if _is_tokensflow(argv, "upload"):
                self.commands.append(argv)
                return self._output(b"uploaded=0 duplicates=4\n", kwargs)
            return super().run(argv, **kwargs)

    docker = DuplicateReplayDocker()
    outcome = DockerSut(docker, relay_factory=FakeRelay).run_arm(
        config,
        Arm.ON,
        paths,
        b"prompt",
        ArtifactStore(paths.result_root),
    )

    codex_index = next(index for index, command in enumerate(docker.commands) if _is_codex_inference(command))
    stop_index = next(
        index
        for index, command in enumerate(docker.commands)
        if any("kill -TERM" in part and "evaluation-daemon.pid" in part for part in command)
    )
    upload_index = next(index for index, command in enumerate(docker.commands) if _is_tokensflow(command, "upload"))
    status_index = next(index for index, command in enumerate(docker.commands) if _is_tokensflow(command, "doctor"))
    cleanup_index = next(
        index
        for index, command in enumerate(docker.commands)
        if command[:3] == ("docker", "rm", "-f") and command[-1] == "powercontext-eval-run-1-on"
    )
    assert codex_index < stop_index < upload_index < status_index < cleanup_index
    assert outcome.tokensflow.daemon_stopped is True
    assert outcome.tokensflow.upload_all_succeeded is True
    assert outcome.tokensflow.queue_caught_up is True
    assert outcome.tokensflow.doctor_rc == 1
    assert outcome.tokensflow.negative_detected is False
    assert outcome.tokensflow.drain_duration_seconds >= 0
    provenance = json.loads((paths.result_root / "tokensflow/provenance.json").read_text())
    assert provenance["daemon_stopped"] is True
    assert provenance["upload_all_succeeded"] is True
    assert provenance["queue_caught_up"] is True
    assert provenance["doctor_rc"] == 1
    assert provenance["negative_detected"] is False
    assert not (paths.runtime / "tokensflow-recovery.json").exists()


@pytest.mark.parametrize(
    ("failure", "doctor_rc", "doctor_output"),
    [
        pytest.param("stop", 1, b"", id="stop-nonzero"),
        pytest.param("upload", 1, b"", id="upload-nonzero"),
        *(
            pytest.param(
                name,
                returncode,
                b"caught up (0 pending files)\n" + output,
                id=f"{name}-doctor-rc{returncode}",
            )
            for name, output in (
                ("pending", b"queue: pending files: 1\n"),
                ("rejected", b"queue: rejected batches: 1\n"),
                ("failed", b"[FAIL] queue: accounting inspection\n"),
                ("blocked", b"queue: blocked ingest batches: 1\n"),
                ("open", b"accounting queue: collector circuit: open failures=1\n"),
            )
            for returncode in (0, 1)
        ),
        pytest.param("missing-caught-up", 1, b"queue inspection unavailable\n", id="rc1-without-caught-up"),
    ],
)
def test_tokensflow_drain_failure_preserves_runtime_and_writes_safe_recovery_marker(
    tmp_path: Path,
    failure: str,
    doctor_rc: int,
    doctor_output: bytes,
) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class DrainFailureDocker(TranscriptDocker):
        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            stop = any("kill -TERM" in part and "evaluation-daemon.pid" in part for part in argv)
            if (failure == "stop" and stop) or (failure == "upload" and _is_tokensflow(argv, "upload")):
                self.commands.append(argv)
                return command_result("private raw failure", returncode=70)
            if doctor_output and _is_tokensflow(argv, "doctor"):
                self.commands.append(argv)
                return self._output(doctor_output, kwargs, returncode=doctor_rc)
            return super().run(argv, **kwargs)

    docker = DrainFailureDocker()
    with pytest.raises(TokensFlowInfrastructureError, match="^TokensFlow drain failed$") as captured:
        DockerSut(docker, relay_factory=FakeRelay).run_arm(
            config,
            Arm.ON,
            paths,
            b"prompt",
            ArtifactStore(paths.result_root),
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert paths.tokensflow_home.is_dir()
    assert paths.pc_home.joinpath("codex-observed.jsonl").is_file()
    marker = paths.runtime / "tokensflow-recovery.json"
    assert json.loads(marker.read_text()) == {
        "reason": "tokensflow_drain_failed",
        "recovery_required": True,
    }
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert "private raw failure" not in marker.read_text()
    if failure == "upload":
        assert not any(_is_tokensflow(command, "doctor") for command in docker.commands)
    assert not any(
        command[:3] == ("docker", "rm", "-f") and command[-1] == "powercontext-eval-run-1-on"
        for command in docker.commands
    )
    retry_paths = make_paths(tmp_path / "retry")
    shutil.copy2(marker, retry_paths.runtime / marker.name)
    with pytest.raises(UnsafeSutConfiguration, match="fresh except"):
        retry_paths.prepare()


def test_tokensflow_drain_commands_share_one_deadline(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")
    times = iter((0.0, 0.0, 0.0, 10.0, 35.0, 60.0))

    class TimeoutCapturingDocker(TranscriptDocker):
        drain_timeouts: list[float]

        def __init__(self) -> None:
            super().__init__()
            self.drain_timeouts = []

        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if any("kill -TERM" in part for part in argv) or _is_tokensflow(argv, "upload"):
                self.drain_timeouts.append(cast(float, kwargs["timeout"]))
            return super().run(argv, **kwargs)

    docker = TimeoutCapturingDocker()
    with pytest.raises(TokensFlowInfrastructureError, match="^TokensFlow drain failed$"):
        DockerSut(docker, relay_factory=FakeRelay, clock=lambda: next(times)).run_arm(
            config,
            Arm.ON,
            paths,
            b"prompt",
            ArtifactStore(paths.result_root),
        )

    assert docker.drain_timeouts == [50.0, 25.0]
    assert not any(_is_tokensflow(command, "doctor") for command in docker.commands)


@pytest.mark.parametrize("phase", ["upload", "doctor"])
def test_tokensflow_drain_command_secret_is_not_retained_or_chained(tmp_path: Path, phase: str) -> None:
    paths = make_paths(tmp_path)
    secret = "tokensflow-drain-command-secret"
    (paths.tokensflow_home / ".tokensflow/credentials.json").write_text(json.dumps({"access_token": secret}))
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class SecretFailureDocker(TranscriptDocker):
        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if phase == "upload" and _is_tokensflow(argv, "upload"):
                self.commands.append(argv)
                raise CommandFailed("raw " + secret, command_result(secret, returncode=70))
            if phase == "doctor" and _is_tokensflow(argv, "doctor"):
                self.commands.append(argv)
                return self._output(
                    b"caught up (0 pending files)\nqueue: blocked ingest: 1\n" + secret.encode(),
                    kwargs,
                    returncode=1,
                )
            return super().run(argv, **kwargs)

    docker = SecretFailureDocker()
    with pytest.raises(TokensFlowInfrastructureError) as captured:
        DockerSut(docker, relay_factory=FakeRelay).run_arm(
            config,
            Arm.ON,
            paths,
            b"prompt",
            ArtifactStore(paths.result_root, forbidden_values=(secret,)),
        )

    assert str(captured.value) == "TokensFlow drain failed"
    assert secret not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert not isinstance(captured.value.__cause__, CommandError)
    retained = b"".join(path.read_bytes() for path in paths.result_root.rglob("*") if path.is_file())
    assert secret.encode() not in retained
    assert secret not in (paths.runtime / "tokensflow-recovery.json").read_text()


def test_tokensflow_commands_redact_only_credentials_and_allow_normal_provenance(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    secret = "tokensflow-command-redaction-secret"
    (paths.tokensflow_home / ".tokensflow/credentials.json").write_text(
        json.dumps(
            {
                "access_token": secret,
                "enabled": True,
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        )
    )
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class CapturingSecretsDocker(TranscriptDocker):
        tokensflow_secrets: list[tuple[str, ...]]

        def __init__(self) -> None:
            super().__init__()
            self.tokensflow_secrets = []

        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if any(part.endswith("/tokensflow") for part in argv) or any(
                "evaluation-daemon.pid" in part for part in argv
            ):
                self.tokensflow_secrets.append(tuple(cast(Sequence[str], kwargs.get("secrets", ()))))
            return super().run(argv, **kwargs)

    docker = CapturingSecretsDocker()
    outcome = DockerSut(docker, relay_factory=FakeRelay).run_arm(
        config,
        Arm.ON,
        paths,
        b"prompt",
        ArtifactStore(paths.result_root),
    )

    assert outcome.tokensflow.identity_match is True
    assert docker.tokensflow_secrets
    assert all(secret in variants for variants in docker.tokensflow_secrets)
    provenance = (paths.result_root / "tokensflow/provenance.json").read_text()
    assert '"identity_match": true' in provenance


@pytest.mark.parametrize(
    ("host_identity", "container_identity"),
    [
        (b"line-one\r\nline-two\r\n", b"line-one\nline-two\n"),
        (b"same", b"same\n"),
    ],
)
def test_tokensflow_identity_normalizes_only_line_endings_and_terminal_newline(
    tmp_path: Path,
    host_identity: bytes,
    container_identity: bytes,
) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    outcome = DockerSut(
        TranscriptDocker(host_identity=host_identity, container_identity=container_identity),
        relay_factory=FakeRelay,
    ).run_arm(config, Arm.OFF, paths, b"prompt", ArtifactStore(paths.result_root))

    assert outcome.tokensflow.identity_match is True


@pytest.mark.parametrize(
    ("host_identity", "container_identity"),
    [
        (b"person-a\n", b"person-b\n"),
        (b"same value\n", b"same value \n"),
        (b"same\rbyte\n", b"same\nbyte\n"),
    ],
)
def test_tokensflow_identity_mismatch_is_sanitized_and_blocks_codex(
    tmp_path: Path,
    host_identity: bytes,
    container_identity: bytes,
) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")
    docker = TranscriptDocker(host_identity=host_identity, container_identity=container_identity)

    with pytest.raises(Exception, match="identity did not match") as captured:
        DockerSut(docker, relay_factory=FakeRelay).run_arm(
            config,
            Arm.ON,
            paths,
            b"prompt",
            ArtifactStore(paths.result_root),
        )

    assert type(captured.value).__name__ == "TokensFlowInfrastructureError"
    serialized_error = repr(captured.value) + str(captured.value)
    for identity in (host_identity, container_identity):
        text_identity = identity.decode("utf-8")
        assert text_identity.strip() not in serialized_error
        assert all(identity.strip() not in path.read_bytes() for path in paths.result_root.rglob("*") if path.is_file())
    assert not any(_is_codex_inference(command) for command in docker.commands)
    assert not any(_is_tokensflow(command, "daemon") for command in docker.commands)


def test_tokensflow_homes_and_daemon_pid_files_are_distinct_across_arms_and_runs(tmp_path: Path) -> None:
    homes: list[Path] = []
    pid_files: list[Path] = []

    for run_id in ("parallel-run-a", "parallel-run-b"):
        root = tmp_path / run_id
        off_paths = make_paths(root / "off")
        on_paths = make_paths(root / "on")
        config = replace(sut_config(root), run_id=run_id)
        config.codex_binary.write_text("binary")
        config.uv_binary.write_text("binary")
        docker = TranscriptDocker()

        outcomes = DockerSut(docker, relay_factory=FakeRelay).run_pair(
            config,
            paths={Arm.OFF: off_paths, Arm.ON: on_paths},
            prompts={Arm.OFF: b"same", Arm.ON: b"same"},
            stores={
                Arm.OFF: ArtifactStore(off_paths.result_root),
                Arm.ON: ArtifactStore(on_paths.result_root),
            },
        )

        assert (
            sum(
                command[:3] == ("docker", "exec", "-d") and _is_tokensflow(command, "daemon")
                for command in docker.commands
            )
            == 2
        )
        for arm, paths in ((Arm.OFF, off_paths), (Arm.ON, on_paths)):
            homes.append(paths.tokensflow_home)
            pid_files.append(outcomes[arm].tokensflow_daemon.pid_file)

    assert len({path.resolve(strict=False) for path in homes}) == 4
    assert len({path.resolve(strict=False) for path in pid_files}) == 4
    assert all(pid_file.is_relative_to(home) for pid_file, home in zip(pid_files, homes, strict=True))


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "non-executable",
        "unsafe-name",
        "unsafe-path",
        "malformed-version",
        "version-mismatch",
        "whoami-failed",
    ],
)
def test_tokensflow_configuration_or_execution_failure_blocks_codex(tmp_path: Path, failure: str) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")
    docker = TranscriptDocker(
        fail_at="whoami" if failure == "whoami-failed" else None,
        host_tokensflow_version=b"not-a-version\n" if failure == "malformed-version" else b"tokensflow 1.0.16\n",
        container_tokensflow_version=b"tokensflow 9.9.9\n" if failure == "version-mismatch" else None,
    )
    if failure == "missing":
        object.__setattr__(config, "tokensflow_binary", tmp_path / "missing-tokensflow")
    elif failure == "non-executable":
        config.tokensflow_binary.chmod(0o600)
    elif failure == "unsafe-name":
        unexpected = tmp_path / "unexpected-tool"
        unexpected.write_text("binary")
        unexpected.chmod(0o755)
        object.__setattr__(config, "tokensflow_binary", unexpected)
    elif failure == "unsafe-path":
        nested = tmp_path / "nested"
        nested.mkdir()
        object.__setattr__(config, "tokensflow_binary", nested / ".." / "tokensflow")

    with pytest.raises(TokensFlowInfrastructureError) as captured:
        DockerSut(docker, relay_factory=FakeRelay).run_arm(
            config,
            Arm.ON,
            paths,
            b"prompt",
            ArtifactStore(paths.result_root),
        )

    assert "TokensFlow" in str(captured.value)
    assert os.fspath(config.tokensflow_binary) not in str(captured.value)
    assert not any(_is_codex_inference(command) for command in docker.commands)


@pytest.mark.parametrize("unsafe", [Path("tokensflow"), Path("/tmp/to\0kensflow")])
def test_tokensflow_binary_path_validation_is_a_sanitized_infrastructure_error(
    tmp_path: Path,
    unsafe: Path,
) -> None:
    config = sut_config(tmp_path)

    with pytest.raises(TokensFlowInfrastructureError) as captured:
        replace(config, tokensflow_binary=unsafe)

    assert os.fspath(unsafe) not in str(captured.value)


def test_missing_tokensflow_arm_home_is_a_sanitized_infrastructure_error(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    object.__setattr__(paths, "tokensflow_home", None)
    config = sut_config(tmp_path)

    with pytest.raises(TokensFlowInfrastructureError, match="TokensFlow inputs"):
        DockerSut(TranscriptDocker(), relay_factory=FakeRelay).run_arm(
            config,
            Arm.ON,
            paths,
            b"prompt",
            ArtifactStore(paths.result_root),
        )


@pytest.mark.parametrize("tokensflow_home", [Path("relative-home"), Path("/outside/runtime/tokensflow-home")])
def test_tokensflow_arm_home_path_validation_is_a_sanitized_infrastructure_error(
    tmp_path: Path,
    tokensflow_home: Path,
) -> None:
    paths = make_paths(tmp_path)

    with pytest.raises(TokensFlowInfrastructureError):
        replace(paths, tokensflow_home=tokensflow_home)


@pytest.mark.parametrize("phase", ["capture", "detached-exec", "readiness"])
def test_tokensflow_command_failures_are_sanitized_block_codex_and_cleanup(
    tmp_path: Path,
    phase: str,
) -> None:
    secret = "tokensflow-command-failure-secret"
    paths = make_paths(tmp_path)
    (paths.tokensflow_home / ".tokensflow/credentials.json").write_text(json.dumps({"access_token": secret}))
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class AdversarialDocker(TranscriptDocker):
        tokensflow_secrets: list[tuple[str, ...]]

        def __init__(self) -> None:
            super().__init__()
            self.tokensflow_secrets = []

        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            detached = argv[:3] == ("docker", "exec", "-d") and _is_tokensflow(argv, "daemon")
            readiness = not detached and any("evaluation-daemon.pid" in part for part in argv)
            capture = argv == (os.fspath(config.tokensflow_binary), "whoami")
            related = any(part.endswith("/tokensflow") for part in argv) or readiness
            if related:
                self.tokensflow_secrets.append(tuple(cast(Sequence[str], kwargs.get("secrets", ()))))
            if (
                (phase == "capture" and capture)
                or (phase == "detached-exec" and detached)
                or (phase == "readiness" and readiness)
            ):
                self.commands.append(argv)
                result = command_result(secret, returncode=70, stderr=secret)
                if phase != "readiness":
                    raise CommandFailed(secret, result)
                return result
            return super().run(argv, **kwargs)

    clock_values = iter((0.0, 6.0, 6.0))
    docker = AdversarialDocker()
    if phase == "readiness":
        sut = DockerSut(
            docker,
            relay_factory=FakeRelay,
            clock=lambda: next(clock_values),
            sleeper=lambda _seconds: None,
        )
    else:
        sut = DockerSut(docker, relay_factory=FakeRelay)

    with pytest.raises(TokensFlowInfrastructureError) as captured:
        sut.run_arm(
            config,
            Arm.ON,
            paths,
            b"prompt",
            ArtifactStore(paths.result_root),
        )

    expected = "TokensFlow command failed" if phase == "capture" else "TokensFlow daemon failed to start"
    assert str(captured.value) == expected
    assert captured.value.__context__ is None
    current: BaseException | None = captured.value
    while current is not None:
        assert secret not in repr(current)
        assert secret not in str(current)
        assert secret not in repr(getattr(current, "result", None))
        current = current.__cause__ or current.__context__
    assert docker.tokensflow_secrets
    assert all(secret in variants for variants in docker.tokensflow_secrets)
    assert not any(_is_codex_inference(command) for command in docker.commands)
    assert ("docker", "rm", "-f", "powercontext-eval-run-1-on") in docker.commands
    retained = b"".join(path.read_bytes() for path in paths.result_root.rglob("*") if path.is_file())
    assert secret.encode() not in retained


def test_pre_exec_shell_pid_never_satisfies_tokensflow_daemon_readiness(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class PreExecShellDocker(TranscriptDocker):
        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            readiness = argv[:3] == ("docker", "exec", "powercontext-eval-run-1-on") and any(
                "evaluation-daemon.pid" in part for part in argv
            )
            if readiness:
                self.commands.append(argv)
                is_identity_probe = any("/proc/" in part or "readlink" in part for part in argv)
                return command_result("", returncode=1 if is_identity_probe else 0)
            return super().run(argv, **kwargs)

    clock_values = iter((0.0, 6.0, 6.0))
    docker = PreExecShellDocker()
    sut = DockerSut(
        docker,
        relay_factory=FakeRelay,
        clock=lambda: next(clock_values),
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(TokensFlowInfrastructureError, match="daemon failed to start") as captured:
        sut.run_arm(config, Arm.ON, paths, b"prompt", ArtifactStore(paths.result_root))

    assert captured.value.__context__ is None
    assert not any(_is_codex_inference(command) for command in docker.commands)


def test_contract_smoke_propagates_dynamic_tokensflow_snapshot_and_secret_boundary(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text('{"token":"codex-smoke-secret"}')
    tokensflow_user_home = tmp_path / "tokensflow-profile"
    tokensflow_config = tokensflow_user_home / ".tokensflow"
    tokensflow_config.mkdir(parents=True)
    (tokensflow_config / "credentials.json").write_text('{"token":"tokensflow-smoke-secret"}')
    tokensflow_binary = tmp_path / "tools/tokensflow"
    tokensflow_binary.parent.mkdir()
    tokensflow_binary.write_text("binary")
    tokensflow_binary.chmod(0o755)
    observed: dict[str, object] = {}

    class CapturingSut:
        def run_pair(
            self,
            config: SutConfig,
            *,
            paths: dict[Arm, ArmPaths],
            prompts: dict[Arm, bytes],
            stores: dict[Arm, ArtifactStore],
        ) -> dict[Arm, SimpleNamespace]:
            observed.update(config=config, paths=paths, prompts=prompts, stores=stores)
            for arm in (Arm.OFF, Arm.ON):
                assert paths[arm].tokensflow_home is not None
                assert paths[arm].tokensflow_home.is_relative_to(paths[arm].runtime)
                assert (paths[arm].tokensflow_home / ".tokensflow/credentials.json").read_text() == (
                    '{"token":"tokensflow-smoke-secret"}'
                )
                with pytest.raises(SecretDetected):
                    stores[arm].write_text("codex-leak.txt", "codex-smoke-secret")
                with pytest.raises(SecretDetected):
                    stores[arm].write_text("tokensflow-leak.txt", "tokensflow-smoke-secret")
            return {
                Arm.OFF: SimpleNamespace(
                    evidence=SimpleNamespace(prompt_sources=0),
                    tokensflow=SimpleNamespace(
                        as_dict=lambda: {
                            "identity_match": True,
                            "daemon_started": True,
                            "daemon_stopped": True,
                            "upload_all_succeeded": True,
                            "queue_caught_up": True,
                        }
                    ),
                ),
                Arm.ON: SimpleNamespace(
                    evidence=SimpleNamespace(prompt_sources=1),
                    tokensflow=SimpleNamespace(
                        as_dict=lambda: {
                            "identity_match": True,
                            "daemon_started": True,
                            "daemon_stopped": True,
                            "upload_all_succeeded": True,
                            "queue_caught_up": True,
                        }
                    ),
                ),
            }

    result = run_codex_contract_smoke(
        run_root=os.fspath(tmp_path / "run"),
        task_image="fixture:image",
        codex_bin=os.fspath(tmp_path / "tools/codex"),
        tokensflow_bin=os.fspath(tokensflow_binary),
        tokensflow_user_home=os.fspath(tokensflow_user_home),
        uv_bin=os.fspath(tmp_path / "tools/uv"),
        powercontext_source=os.fspath(source),
        powercontext_sha="a" * 40,
        auth_json=os.fspath(auth),
        proxy_url="http://127.0.0.1:7890",
        sut_factory=lambda _process: CapturingSut(),  # type: ignore[arg-type]
    )

    config = cast(SutConfig, observed["config"])
    paths = cast(dict[Arm, ArmPaths], observed["paths"])
    assert config.tokensflow_binary == tokensflow_binary
    assert paths[Arm.OFF].tokensflow_home != paths[Arm.ON].tokensflow_home
    assert result["off_prompt_sources"] == 0
    assert result["on_prompt_sources"] == 1
    assert result["tokensflow"] == {
        "off": {
            "identity_match": True,
            "daemon_started": True,
            "daemon_stopped": True,
            "upload_all_succeeded": True,
            "queue_caught_up": True,
        },
        "on": {
            "identity_match": True,
            "daemon_started": True,
            "daemon_stopped": True,
            "upload_all_succeeded": True,
            "queue_caught_up": True,
        },
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "tokensflow-smoke-secret" not in serialized
    assert os.fspath(tokensflow_user_home) not in serialized


@pytest.mark.parametrize("profile", ["missing", "symlink"])
def test_contract_smoke_translates_unsafe_tokensflow_profile_to_sanitized_error(
    tmp_path: Path,
    profile: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    auth = tmp_path / "auth.json"
    auth.write_text('{"token":"codex-smoke-secret"}')
    tokensflow_user_home = tmp_path / "tokensflow-profile"
    tokensflow_config = tokensflow_user_home / ".tokensflow"
    tokensflow_config.mkdir(parents=True)
    credentials = tokensflow_config / "credentials.json"
    if profile == "symlink":
        external = tmp_path / "external-credentials.json"
        external.write_text('{"token":"do-not-retain"}')
        credentials.symlink_to(external)

    with pytest.raises(TokensFlowInfrastructureError) as captured:
        run_codex_contract_smoke(
            run_root=os.fspath(tmp_path / "run"),
            task_image="fixture:image",
            codex_bin=os.fspath(tmp_path / "tools/codex"),
            tokensflow_bin=os.fspath(tmp_path / "tools/tokensflow"),
            tokensflow_user_home=os.fspath(tokensflow_user_home),
            uv_bin=os.fspath(tmp_path / "tools/uv"),
            powercontext_source=os.fspath(source),
            powercontext_sha="a" * 40,
            auth_json=os.fspath(auth),
            proxy_url="http://127.0.0.1:7890",
            sut_factory=lambda _process: pytest.fail("SUT must not run"),
        )

    assert str(captured.value) == "TokensFlow profile snapshot failed"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is not None
    assert os.fspath(tokensflow_user_home) not in str(captured.value)


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
            tokensflow_binary=tmp_path / "tokensflow",
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


def test_codex_failure_filters_tokensflow_secret_from_process_and_retained_artifacts(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    secret = "tokensflow-codex-stderr-secret"
    credentials = paths.tokensflow_home / ".tokensflow/credentials.json"
    credentials.write_text(json.dumps({"access_token": secret}))
    config = sut_config(tmp_path)
    config.codex_binary.write_text("binary")
    config.uv_binary.write_text("binary")

    class LeakingCodexDocker(TranscriptDocker):
        codex_secrets: tuple[str, ...] = ()

        def run(self, argv: tuple[str, ...], **kwargs: object) -> CommandResult:
            if _is_codex_inference(argv):
                self.commands.append(argv)
                self.codex_secrets = tuple(cast(Sequence[str], kwargs.get("secrets", ())))
                return ProcessRunner().run(
                    (
                        sys.executable,
                        "-c",
                        "import sys; sys.stderr.write(sys.argv[1]); raise SystemExit(70)",
                        secret,
                    ),
                    cwd=cast(Path, kwargs["cwd"]),
                    timeout=cast(float, kwargs["timeout"]),
                    secrets=self.codex_secrets,
                    input_bytes=cast(bytes, kwargs["input_bytes"]),
                    stdout_sink=cast(BinaryIO, kwargs["stdout_sink"]),
                )
            return super().run(argv, **kwargs)

    docker = LeakingCodexDocker()
    with pytest.raises(CodexInfrastructureError) as captured:
        DockerSut(docker, relay_factory=FakeRelay).run_arm(
            config,
            Arm.ON,
            paths,
            b"prompt",
            ArtifactStore(paths.result_root),
        )

    assert secret in docker.codex_secrets
    errors: list[BaseException] = []
    current: BaseException | None = captured.value
    while current is not None:
        errors.append(current)
        current = current.__cause__ or current.__context__
    assert all(secret not in repr(error) and secret not in str(error) for error in errors)
    assert all(secret not in repr(getattr(error, "result", None)) for error in errors)
    assert all(secret.encode() not in path.read_bytes() for path in paths.result_root.rglob("*") if path.is_file())


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
        selected.mkdir(parents=True, exist_ok=target == "runtime")
        (selected / "old").write_text("stale")
    else:
        destination = tmp_path / f"{target}-elsewhere"
        destination.mkdir()
        selected.parent.mkdir(parents=True, exist_ok=True)
        if target == "runtime":
            shutil.rmtree(selected)
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
            if not self.timed_out and "/runtime/pc-env/bin/powercontext" in argv and "doctor" in argv:
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

    readiness_probes = [
        command for command in docker.commands if "/runtime/pc-env/bin/powercontext" in command and "doctor" in command
    ]
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
