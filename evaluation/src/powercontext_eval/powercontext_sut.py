"""Disposable Docker lifecycle for balanced PowerContext treatments."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import signal
import socket
import stat
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from powercontext_eval.artifacts import ArtifactStore
from powercontext_eval.codex import EXPECTED_CODEX_VERSION, CodexInvocation, CodexOutcome, CodexRunner
from powercontext_eval.errors import PowerContextEvalError
from powercontext_eval.models import Arm
from powercontext_eval.process import CommandResult, ProcessRunner

PLUGIN_ID = "powercontext@powercontext"
_SAFE_RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_SHA = re.compile(r"[0-9a-f]{40}")
_CONTAINER_UID_GID = "2950:100"


class InvalidTreatment(PowerContextEvalError):
    """Observed evidence does not prove the requested treatment."""


class UnsafeSutConfiguration(PowerContextEvalError):
    """A SUT value could escape its owned resource boundary."""


@dataclass(frozen=True)
class TreatmentEvidence:
    """Strict post-run treatment evidence."""

    plugin_installed: bool
    plugin_id: str
    plugin_version: str
    plugin_checkout_sha: str
    server_ready: bool
    prompt_sources: int
    mcp_requests: int
    scope_id: str

    def __post_init__(self) -> None:
        if type(self.plugin_installed) is not bool or type(self.server_ready) is not bool:
            raise TypeError("Treatment booleans must be exact bool values")
        if not self.plugin_id or not self.plugin_version or _SHA.fullmatch(self.plugin_checkout_sha) is None:
            raise ValueError("Treatment plugin provenance is invalid")
        if (
            isinstance(self.prompt_sources, bool)
            or not isinstance(self.prompt_sources, int)
            or self.prompt_sources < 0
            or isinstance(self.mcp_requests, bool)
            or not isinstance(self.mcp_requests, int)
            or self.mcp_requests < 0
        ):
            raise ValueError("Treatment counters must be non-negative integers")
        if not self.scope_id:
            raise ValueError("Treatment scope must not be empty")

    def as_dict(self) -> dict[str, object]:
        return {
            "plugin_installed": self.plugin_installed,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "plugin_checkout_sha": self.plugin_checkout_sha,
            "server_ready": self.server_ready,
            "prompt_sources": self.prompt_sources,
            "mcp_requests": self.mcp_requests,
            "scope_id": self.scope_id,
        }

    @classmethod
    def from_json(cls, raw: str) -> TreatmentEvidence:
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError
            return cls(**value)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise InvalidTreatment("Treatment evidence is malformed") from error


def validate_treatment(
    arm: Arm,
    run_id: str,
    evidence: TreatmentEvidence,
    *,
    expected_plugin_version: str,
    expected_checkout_sha: str,
) -> None:
    """Fail closed unless the exact expected treatment is proven."""

    expected_scope = f"eval:{run_id}:{arm.value}"
    common = (
        evidence.plugin_installed
        and evidence.plugin_id == PLUGIN_ID
        and evidence.plugin_version == expected_plugin_version
        and evidence.plugin_checkout_sha == expected_checkout_sha
        and evidence.server_ready
        and evidence.scope_id == expected_scope
    )
    activity = (
        evidence.prompt_sources >= 1 if arm is Arm.ON else evidence.prompt_sources == 0 and evidence.mcp_requests == 0
    )
    if not common or not activity:
        raise InvalidTreatment("Treatment evidence does not match the requested arm")


@dataclass(frozen=True)
class ProxyRelayConfig:
    """Credential-free loopback proxy upstream."""

    url: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        try:
            port = parsed.port
        except ValueError as error:
            raise UnsafeSutConfiguration("Proxy upstream is invalid") from error
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise UnsafeSutConfiguration("Proxy upstream must be a credential-free loopback URL")


@dataclass(frozen=True)
class ContainerLimits:
    cpus: str = "2"
    memory: str = "4g"
    pids: int = 256

    def __post_init__(self) -> None:
        if re.fullmatch(r"[1-9][0-9]*(?:\.[0-9]+)?", self.cpus) is None:
            raise UnsafeSutConfiguration("CPU limit is unsafe")
        if re.fullmatch(r"[1-9][0-9]*[kmg]", self.memory.lower()) is None or not 1 <= self.pids <= 4096:
            raise UnsafeSutConfiguration("Container limits are unsafe")


@dataclass(frozen=True)
class SutConfig:
    """Pinned inputs shared by both treatment arms."""

    run_id: str
    task_image: str
    codex_binary: Path
    uv_binary: Path
    source_checkout: Path
    plugin_checkout_sha: str
    proxy: ProxyRelayConfig
    limits: ContainerLimits = ContainerLimits()
    plugin_version: str = "0.1.0"
    codex_timeout: float = 3600

    def __post_init__(self) -> None:
        if _SAFE_RUN_ID.fullmatch(self.run_id) is None:
            raise UnsafeSutConfiguration("Run id is unsafe")
        if (
            not self.task_image
            or self.task_image.startswith("-")
            or any(char in self.task_image for char in "\0 \t\r\n")
        ):
            raise UnsafeSutConfiguration("Task image is unsafe")
        if _SHA.fullmatch(self.plugin_checkout_sha) is None:
            raise UnsafeSutConfiguration("Plugin checkout SHA is unsafe")
        for path in (self.codex_binary, self.uv_binary, self.source_checkout):
            if not path.is_absolute() or "\0" in os.fspath(path):
                raise UnsafeSutConfiguration("SUT paths must be absolute")
        if self.codex_timeout <= 0:
            raise UnsafeSutConfiguration("Codex timeout must be positive")


@dataclass(frozen=True)
class ArmPaths:
    """Ephemeral inputs and retained result root for one arm."""

    source: Path
    auth_source: Path
    workspace: Path
    runtime: Path
    codex_home: Path
    pc_home: Path
    result_root: Path

    def __post_init__(self) -> None:
        paths = (
            self.source,
            self.auth_source,
            self.workspace,
            self.runtime,
            self.codex_home,
            self.pc_home,
            self.result_root,
        )
        if any(not path.is_absolute() for path in paths):
            raise UnsafeSutConfiguration("Arm paths must be absolute")
        if self.codex_home.is_relative_to(self.result_root) or self.pc_home.is_relative_to(self.result_root):
            raise UnsafeSutConfiguration("Private homes must remain outside retained results")
        if not self.codex_home.is_relative_to(self.runtime) or not self.pc_home.is_relative_to(self.runtime):
            raise UnsafeSutConfiguration("Private homes must remain within the ephemeral runtime")

    def prepare(self) -> None:
        for path in (self.workspace, self.runtime, self.codex_home, self.pc_home):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def copy_auth(self) -> Path:
        """Copy only auth.json through no-follow descriptors at mode 0600."""

        self.codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.codex_home / "auth.json"
        source_fd = os.open(self.auth_source, os.O_RDONLY | os.O_NOFOLLOW)
        destination_fd = -1
        try:
            metadata = os.fstat(source_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsafeSutConfiguration("Auth source must be a regular file")
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            while chunk := os.read(source_fd, 64 * 1024):
                os.write(destination_fd, chunk)
            os.fsync(destination_fd)
        except FileExistsError as error:
            raise UnsafeSutConfiguration("Ephemeral auth destination already exists") from error
        finally:
            os.close(source_fd)
            if destination_fd >= 0:
                os.close(destination_fd)
        os.chmod(destination, 0o600, follow_symlinks=False)
        return destination


class CommandRunner(Protocol):
    def run(self, argv: tuple[str, ...], **kwargs: Any) -> CommandResult: ...


class ProxyRelay(Protocol):
    def start(self, gateway: str, upstream: ProxyRelayConfig) -> str: ...

    def stop(self) -> None: ...


class SocatProxyRelay:
    """One exact host process bound only to an internal bridge gateway."""

    def __init__(self, *, executable: str = "socat", readiness_timeout: float = 5.0) -> None:
        self._executable = executable
        self._timeout = readiness_timeout
        self._process: subprocess.Popen[bytes] | None = None

    def start(self, gateway: str, upstream: ProxyRelayConfig) -> str:
        address = _validated_gateway(gateway)
        parsed = urlsplit(upstream.url)
        assert parsed.hostname is not None and parsed.port is not None
        port = _reserve_port(address)
        argv = (
            self._executable,
            f"TCP-LISTEN:{port},bind={address},fork,reuseaddr",
            f"TCP:{parsed.hostname}:{parsed.port}",
        )
        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
                shell=False,
                env={"PATH": os.environ.get("PATH", "")},
            )
            self._wait_ready(address, port)
        except BaseException:
            self.stop()
            raise
        return f"{parsed.scheme}://{address}:{port}"

    def _wait_ready(self, gateway: str, port: int) -> None:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            process = self._process
            if process is None or process.poll() is not None:
                raise UnsafeSutConfiguration("Proxy relay exited before readiness")
            try:
                with socket.create_connection((gateway, port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.02)
        raise UnsafeSutConfiguration("Proxy relay readiness timed out")

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if process.stderr is not None:
            process.stderr.close()


@dataclass(frozen=True)
class SutOutcome:
    codex: CodexOutcome
    evidence: TreatmentEvidence


class _DockerExecRunner(ProcessRunner):
    def __init__(self, runner: CommandRunner, container: str) -> None:
        self._delegate = runner
        self._container = container

    def run(self, argv: Any, **kwargs: Any) -> CommandResult:
        return self._delegate.run(("docker", "exec", "-i", self._container, *tuple(argv)), **kwargs)


class DockerSut:
    """Execute one arm while owning only run-prefixed Docker resources."""

    def __init__(
        self,
        docker: CommandRunner,
        *,
        relay_factory: Callable[[], ProxyRelay] = SocatProxyRelay,
    ) -> None:
        self._docker = docker
        self._relay_factory = relay_factory

    def network_gateway(self, network: str, cwd: Path) -> str:
        if (
            not network.startswith("powercontext-eval-")
            or _SAFE_RUN_ID.fullmatch(network.removeprefix("powercontext-eval-")) is None
        ):
            raise UnsafeSutConfiguration("Network name is unsafe")
        result = self._docker.run(("docker", "network", "inspect", network), cwd=cwd, timeout=30)
        try:
            value = json.loads(result.stdout)
            gateway = value[0]["IPAM"]["Config"][0]["Gateway"]
            if not isinstance(gateway, str):
                raise TypeError
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as error:
            raise UnsafeSutConfiguration("Docker network inspect did not provide one gateway") from error
        return _validated_gateway(gateway)

    def run_arm(
        self,
        config: SutConfig,
        arm: Arm,
        paths: ArmPaths,
        prompt: bytes,
        store: ArtifactStore,
    ) -> SutOutcome:
        with self._run_network(config, paths.runtime) as (network, relay_url):
            return self._execute_arm(config, arm, paths, prompt, store, network, relay_url)

    def run_pair(
        self,
        config: SutConfig,
        *,
        paths: Mapping[Arm, ArmPaths],
        prompts: Mapping[Arm, bytes],
        stores: Mapping[Arm, ArtifactStore],
    ) -> Mapping[Arm, SutOutcome]:
        """Run OFF then ON serially through one exact network and relay URL."""

        if set(paths) != {Arm.OFF, Arm.ON} or set(prompts) != {Arm.OFF, Arm.ON} or set(stores) != {Arm.OFF, Arm.ON}:
            raise UnsafeSutConfiguration("A treatment pair requires exactly OFF and ON inputs")
        with self._run_network(config, paths[Arm.OFF].runtime) as (network, relay_url):
            outcomes: dict[Arm, SutOutcome] = {}
            for arm in (Arm.OFF, Arm.ON):
                outcomes[arm] = self._execute_arm(
                    config,
                    arm,
                    paths[arm],
                    prompts[arm],
                    stores[arm],
                    network,
                    relay_url,
                )
            return outcomes

    @contextmanager
    def _run_network(self, config: SutConfig, cwd: Path) -> Iterator[tuple[str, str]]:
        network = f"powercontext-eval-{config.run_id}"
        relay = self._relay_factory()
        network_created = False
        try:
            self._docker.run(
                (
                    "docker",
                    "network",
                    "create",
                    "--internal",
                    "--label",
                    f"powercontext-eval.run={config.run_id}",
                    network,
                ),
                cwd=cwd,
                timeout=30,
            )
            network_created = True
            gateway = self.network_gateway(network, cwd)
            relay_url = relay.start(gateway, config.proxy)
            yield network, relay_url
        finally:
            relay.stop()
            if network_created:
                self._docker.run(
                    ("docker", "network", "rm", network),
                    cwd=cwd,
                    timeout=30,
                    check=False,
                )

    def _execute_arm(
        self,
        config: SutConfig,
        arm: Arm,
        paths: ArmPaths,
        prompt: bytes,
        store: ArtifactStore,
        network: str,
        relay_url: str,
    ) -> SutOutcome:
        container = f"{network}-{arm.value}"
        container_started = False
        try:
            paths.prepare()
            auth = paths.copy_auth()
            self._initialize_workspace(config, arm, paths)
            self._assign_arm_ownership(config, paths)
            self._prewarm(config, arm, paths, network, relay_url, auth)
            self._start_container(config, arm, paths, network, container, relay_url, auth)
            container_started = True
            self._verify_codex_version(container, paths, store)
            self._readiness(container, paths)
            plugin = self._plugin_list(container, paths)
            codex = CodexRunner(_DockerExecRunner(self._docker, container)).run(
                CodexInvocation(arm, inside_disposable_container=True, executable="/tools/codex"),
                prompt=prompt,
                cwd=paths.workspace,
                store=store,
                timeout=config.codex_timeout,
                env={
                    "CODEX_HOME": "/runtime/codex-home",
                    "POWERCONTEXT_HOME": "/runtime/pc-home",
                    "POWERCONTEXT_CODEX_SCOPE_ID": f"eval:{config.run_id}:{arm.value}",
                },
            )
            evidence = self._evidence(config, arm, container, paths, plugin)
            if plugin != (PLUGIN_ID, config.plugin_version):
                raise InvalidTreatment("Isolated Codex home does not contain the exact expected plugin")
            validate_treatment(
                arm,
                config.run_id,
                evidence,
                expected_plugin_version=config.plugin_version,
                expected_checkout_sha=config.plugin_checkout_sha,
            )
            logs = self._docker.run(
                ("docker", "logs", container),
                cwd=paths.runtime,
                timeout=30,
                check=False,
            )
            store.write_text("powercontext/server.log", logs.stdout + logs.stderr)
            store.write_json("powercontext/treatment.json", evidence.as_dict())
            return SutOutcome(codex, evidence)
        finally:
            if container_started:
                self._docker.run(
                    ("docker", "rm", "-f", container),
                    cwd=paths.runtime,
                    timeout=30,
                    check=False,
                )

    def _initialize_workspace(self, config: SutConfig, arm: Arm, paths: ArmPaths) -> None:
        name = f"powercontext-eval-{config.run_id}-{arm.value}-init"
        created = False
        try:
            self._docker.run(
                (
                    "docker",
                    "create",
                    "--name",
                    name,
                    "--label",
                    f"powercontext-eval.run={config.run_id}",
                    config.task_image,
                ),
                cwd=paths.runtime,
                timeout=60,
            )
            created = True
            self._docker.run(
                ("docker", "cp", f"{name}:/workspace/.", os.fspath(paths.workspace)),
                cwd=paths.runtime,
                timeout=300,
            )
        finally:
            if created:
                self._docker.run(("docker", "rm", "-f", name), cwd=paths.runtime, timeout=30, check=False)

    def _assign_arm_ownership(self, config: SutConfig, paths: ArmPaths) -> None:
        """Use a networkless, capability-minimal helper for exact owned mounts."""

        self._docker.run(
            (
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "CHOWN",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "0:0",
                "--mount",
                f"type=bind,src={paths.workspace},dst=/workspace",
                "--mount",
                f"type=bind,src={paths.runtime},dst=/runtime",
                "--entrypoint",
                "/bin/chown",
                config.task_image,
                "--recursive",
                _CONTAINER_UID_GID,
                "/workspace",
                "/runtime",
            ),
            cwd=paths.runtime,
            timeout=300,
        )

    def _prewarm(
        self,
        config: SutConfig,
        arm: Arm,
        paths: ArmPaths,
        network: str,
        relay_url: str,
        auth: Path,
    ) -> None:
        del arm, auth
        command = (
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            _CONTAINER_UID_GID,
            "--cpus",
            config.limits.cpus,
            "--memory",
            config.limits.memory,
            "--pids-limit",
            str(config.limits.pids),
            "--network",
            network,
            "--mount",
            f"type=bind,src={config.source_checkout},dst=/source,readonly",
            "--mount",
            f"type=bind,src={paths.runtime},dst=/runtime",
            "--mount",
            f"type=bind,src={config.uv_binary},dst=/tools/uv,readonly",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=512m",
            "-e",
            f"HTTPS_PROXY={relay_url}",
            "-e",
            "UV_PROJECT_ENVIRONMENT=/runtime/pc-env",
            config.task_image,
            "/tools/uv",
            "sync",
            "--frozen",
            "--project",
            "/source",
            "--extra",
            "server",
        )
        self._docker.run(command, cwd=paths.runtime, timeout=900)
        setup_common = (
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            _CONTAINER_UID_GID,
            "--cpus",
            config.limits.cpus,
            "--memory",
            config.limits.memory,
            "--pids-limit",
            str(config.limits.pids),
            "--network",
            network,
            "--mount",
            f"type=bind,src={config.source_checkout},dst=/source,readonly",
            "--mount",
            f"type=bind,src={paths.runtime},dst=/runtime",
            "--mount",
            f"type=bind,src={config.codex_binary},dst=/tools/codex,readonly",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "-e",
            "CODEX_HOME=/runtime/codex-home",
            config.task_image,
            "/tools/codex",
            "plugin",
        )
        self._docker.run(
            (*setup_common, "marketplace", "add", "/source", "--json"),
            cwd=paths.runtime,
            timeout=120,
        )
        self._docker.run(
            (*setup_common, "add", PLUGIN_ID, "--json"),
            cwd=paths.runtime,
            timeout=120,
        )

    def _start_container(
        self,
        config: SutConfig,
        arm: Arm,
        paths: ArmPaths,
        network: str,
        container: str,
        relay_url: str,
        auth: Path,
    ) -> None:
        scope = f"eval:{config.run_id}:{arm.value}"
        command = (
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--label",
            f"powercontext-eval.run={config.run_id}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            _CONTAINER_UID_GID,
            "--cpus",
            config.limits.cpus,
            "--memory",
            config.limits.memory,
            "--pids-limit",
            str(config.limits.pids),
            "--network",
            network,
            "--mount",
            f"type=bind,src={paths.workspace},dst=/workspace",
            "--mount",
            f"type=bind,src={paths.runtime},dst=/runtime",
            "--mount",
            f"type=bind,src={config.source_checkout},dst=/source,readonly",
            "--mount",
            f"type=bind,src={config.codex_binary},dst=/tools/codex,readonly",
            "--mount",
            f"type=bind,src={config.uv_binary},dst=/tools/uv,readonly",
            "--mount",
            f"type=bind,src={auth},dst=/runtime/codex-home/auth.json,readonly",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=1g",
            "-e",
            f"HTTPS_PROXY={relay_url}",
            "-e",
            f"HTTP_PROXY={relay_url}",
            "-e",
            "CODEX_HOME=/runtime/codex-home",
            "-e",
            "POWERCONTEXT_HOME=/runtime/pc-home",
            "-e",
            f"POWERCONTEXT_CODEX_SCOPE_ID={scope}",
            "-e",
            "PATH=/tools:/runtime/pc-env/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            config.task_image,
            "/runtime/pc-env/bin/powercontext",
            "server",
            "run",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        )
        self._docker.run(command, cwd=paths.runtime, timeout=60)

    def _readiness(self, container: str, paths: ArmPaths) -> None:
        self._docker.run(
            (
                "docker",
                "exec",
                container,
                "/runtime/pc-env/bin/powercontext",
                "doctor",
                "--server-url",
                "http://127.0.0.1:8000",
                "--json",
            ),
            cwd=paths.runtime,
            timeout=60,
        )

    def _verify_codex_version(self, container: str, paths: ArmPaths, store: ArtifactStore) -> None:
        result = self._docker.run(
            ("docker", "exec", container, "/tools/codex", "--version"),
            cwd=paths.runtime,
            timeout=30,
        )
        match = re.fullmatch(r"codex-cli ([0-9]+\.[0-9]+\.[0-9]+)\n?", result.stdout)
        if match is None or match.group(1) != EXPECTED_CODEX_VERSION:
            raise InvalidTreatment("Codex CLI version does not match the pinned experiment")
        store.write_json(
            "codex/provenance.json",
            {"actual_version": match.group(1), "expected_version": EXPECTED_CODEX_VERSION},
        )

    def _plugin_list(self, container: str, paths: ArmPaths) -> tuple[str, str]:
        result = self._docker.run(
            ("docker", "exec", container, "/tools/codex", "plugin", "list", "--json"),
            cwd=paths.runtime,
            timeout=30,
        )
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict) or value.get("available") != []:
                raise TypeError
            plugins = value["installed"]
            if not isinstance(plugins, list) or len(plugins) != 1 or not isinstance(plugins[0], dict):
                raise TypeError
            plugin = plugins[0]
            plugin_id = plugin["pluginId"]
            version = plugin["version"]
            if (
                not isinstance(plugin_id, str)
                or not plugin_id
                or not isinstance(version, str)
                or not version
                or plugin.get("installed") is not True
            ):
                raise TypeError
        except (json.JSONDecodeError, KeyError, TypeError):
            raise InvalidTreatment("Isolated Codex home must contain exactly one plugin")
        return plugin_id, version

    def _evidence(
        self,
        config: SutConfig,
        arm: Arm,
        container: str,
        paths: ArmPaths,
        plugin: tuple[str, str],
    ) -> TreatmentEvidence:
        scope = f"eval:{config.run_id}:{arm.value}"
        query = (
            "import json,sqlite3,sys;"
            "db=sqlite3.connect('/runtime/pc-home/powercontext.db');"
            "count=db.execute('SELECT COUNT(*) FROM pc_sources WHERE scope_id = ?', (sys.argv[1],)).fetchone()[0];"
            "print(json.dumps({'prompt_sources':count}))"
        )
        result = self._docker.run(
            (
                "docker",
                "exec",
                container,
                "/runtime/pc-env/bin/python",
                "-c",
                query,
                scope,
                "evidence",
            ),
            cwd=paths.runtime,
            timeout=60,
        )
        try:
            raw = json.loads(result.stdout)
            prompt_sources = raw["prompt_sources"]
            if isinstance(prompt_sources, bool) or not isinstance(prompt_sources, int):
                raise TypeError
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise InvalidTreatment("PowerContext SQLite evidence is malformed") from error
        logs = self._docker.run(
            ("docker", "logs", container),
            cwd=paths.runtime,
            timeout=30,
            check=False,
        )
        mcp_requests = sum("/mcp" in line for line in (logs.stdout + logs.stderr).splitlines())
        return TreatmentEvidence(
            plugin_installed=True,
            plugin_id=plugin[0],
            plugin_version=plugin[1],
            plugin_checkout_sha=config.plugin_checkout_sha,
            server_ready=True,
            prompt_sources=prompt_sources,
            mcp_requests=mcp_requests,
            scope_id=scope,
        )


def _validated_gateway(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise UnsafeSutConfiguration("Docker gateway is invalid") from error
    if address.is_loopback or address.is_unspecified or address.is_multicast or not address.is_private:
        raise UnsafeSutConfiguration("Docker gateway must be a private bridge address")
    return str(address)


def _reserve_port(address: str) -> int:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.bind((address, 0))
        port = listener.getsockname()[1]
    return int(port)
