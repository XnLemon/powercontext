"""Codex invocation and JSONL evidence contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from powercontext_eval.artifacts import ArtifactStore
from powercontext_eval.errors import CommandError, PowerContextEvalError
from powercontext_eval.models import Arm
from powercontext_eval.process import CommandResult

EXPECTED_CODEX_VERSION = "0.145.0"


class UnsafeCodexInvocation(PowerContextEvalError):
    """Dangerous Codex flags were requested outside an isolated task container."""


class CodexInfrastructureError(PowerContextEvalError):
    """Codex infrastructure failed before a benchmark patch could be evaluated."""


@dataclass(frozen=True)
class CodexInvocation:
    """The treatment-balanced Codex command."""

    arm: Arm
    inside_disposable_container: bool
    executable: str = "codex"
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "medium"
    expected_version: str = EXPECTED_CODEX_VERSION

    def argv(self) -> tuple[str, ...]:
        """Build the exact invocation, failing closed on host use."""

        if not self.inside_disposable_container:
            raise UnsafeCodexInvocation("Dangerous Codex invocation requires a disposable task container")
        if not self.executable or self.executable.startswith("-") or "\0" in self.executable:
            raise UnsafeCodexInvocation("Codex executable is unsafe")
        if not self.model or self.model.startswith("-") or "\0" in self.model:
            raise UnsafeCodexInvocation("Codex model is unsafe")
        if not self.reasoning_effort or "\0" in self.reasoning_effort:
            raise UnsafeCodexInvocation("Codex reasoning effort is unsafe")
        switch = "--enable" if self.arm is Arm.ON else "--disable"
        return (
            self.executable,
            "exec",
            "--ephemeral",
            "--ignore-rules",
            "--json",
            "--disable",
            "shell_snapshot",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--model",
            self.model,
            "-c",
            f'model_reasoning_effort="{self.reasoning_effort}"',
            switch,
            "plugins",
            "-C",
            "/workspace",
            "-",
        )


@dataclass(frozen=True)
class CodexOutcome:
    """Parsed, retained output from a successful Codex turn."""

    last_message: str
    usage: Mapping[str, int] | None


class CodexProcessRunner(Protocol):
    """Structural child-process adapter used by CodexRunner."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        secrets: Sequence[str] = (),
        input_bytes: bytes | None = None,
    ) -> CommandResult: ...


class CodexRunner:
    """Run Codex through a process adapter and retain only audited artifacts."""

    def __init__(self, process_runner: CodexProcessRunner) -> None:
        self._runner = process_runner

    def run(
        self,
        invocation: CodexInvocation,
        *,
        prompt: bytes,
        cwd: str | Path,
        store: ArtifactStore,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        secrets: tuple[str, ...] = (),
    ) -> CodexOutcome:
        """Send the exact prompt on stdin and parse strict JSONL output."""

        if not isinstance(prompt, bytes):
            raise TypeError("prompt must be exact bytes")
        try:
            result = self._runner.run(
                invocation.argv(),
                cwd=cwd,
                timeout=timeout,
                env=env,
                secrets=secrets,
                input_bytes=prompt,
            )
        except CommandError as error:
            self._retain_process_result(store, error.result)
            raise CodexInfrastructureError(_command_error_kind(error)) from None

        self._retain_process_result(store, result)
        try:
            events = _parse_jsonl(result.stdout)
            last_message = _last_agent_message(events)
            usage = _last_usage(events)
        except (ValueError, TypeError) as error:
            raise CodexInfrastructureError(f"Codex JSONL is malformed: {error}") from None

        store.write_text("codex/last-message.txt", last_message)
        store.write_json("codex/usage.json", dict(usage) if usage is not None else {"status": "N/A"})
        return CodexOutcome(last_message, usage)

    @staticmethod
    def _retain_process_result(store: ArtifactStore, result: CommandResult) -> None:
        store.write_bytes("codex/events.jsonl", result.stdout.encode("utf-8"))
        store.write_bytes("codex/stderr.txt", result.stderr.encode("utf-8"))


def _command_error_kind(error: CommandError) -> str:
    if error.result.returncode == 124:
        return "Codex timed out"
    return f"Codex failed with exit status {error.result.returncode}"


def _parse_jsonl(raw: str) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line:
            raise ValueError(f"empty line {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError(f"invalid JSON on line {line_number}") from None
        if not isinstance(value, dict):
            raise TypeError(f"line {line_number} is not an object")
        events.append(value)
    if not events:
        raise ValueError("empty stream")
    return tuple(events)


def _last_agent_message(events: tuple[dict[str, Any], ...]) -> str:
    messages: list[str] = []
    for event in events:
        if event.get("type") == "agent_message" and isinstance(event.get("message"), str):
            messages.append(event["message"])
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            messages.append(item["text"])
    if not messages:
        raise ValueError("no completed agent message")
    return messages[-1]


def _last_usage(events: tuple[dict[str, Any], ...]) -> Mapping[str, int] | None:
    usage: Mapping[str, int] | None = None
    for event in events:
        if event.get("type") != "turn.completed":
            continue
        raw_usage = event.get("usage")
        if raw_usage is None:
            usage = None
            continue
        if not isinstance(raw_usage, dict):
            raise TypeError("turn.completed usage is not an object")
        parsed: dict[str, int] = {}
        for key, value in raw_usage.items():
            if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("turn.completed usage must contain non-negative integer values")
            parsed[key] = value
        usage = MappingProxyType(parsed)
    return usage
