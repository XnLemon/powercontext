"""Safe, deterministic child-process execution."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from powercontext_eval.errors import CommandFailed, CommandNotFound, CommandTimedOut

_INHERITED_ENVIRONMENT_KEYS = frozenset(
    {
        "ALL_PROXY",
        "GIT_SSL_CAINFO",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "NO_PROXY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "TMPDIR",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)
_NOT_FOUND_RETURN_CODE = 127
_TIMEOUT_RETURN_CODE = 124


@dataclass(frozen=True)
class CommandResult:
    """Sanitized evidence from one child-process invocation."""

    argv: tuple[str, ...]
    cwd: str
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner:
    """Run argv directly with a deliberately narrow inherited environment."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        secrets: Sequence[str] = (),
    ) -> CommandResult:
        """Execute a command without a shell and return redacted output."""

        validated_argv = _validate_argv(argv)
        cwd_text = os.fspath(cwd)
        if "\0" in cwd_text:
            raise ValueError("cwd must not contain NUL")
        child_env = _build_environment(env)
        redactor = _Redactor(secrets)

        try:
            completed = subprocess.run(
                validated_argv,
                cwd=cwd_text,
                env=child_env,
                capture_output=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except FileNotFoundError as error:
            result = _result(
                redactor,
                validated_argv,
                cwd_text,
                _NOT_FOUND_RETURN_CODE,
                b"",
                str(error),
            )
            raise CommandNotFound(_failure_message("Command could not be started", result, redactor), result) from None
        except subprocess.TimeoutExpired as error:
            result = _result(
                redactor,
                validated_argv,
                cwd_text,
                _TIMEOUT_RETURN_CODE,
                error.stdout,
                error.stderr,
            )
            raise CommandTimedOut(_failure_message("Command timed out", result, redactor), result) from None
        except OSError as error:
            result = _result(
                redactor,
                validated_argv,
                cwd_text,
                _NOT_FOUND_RETURN_CODE,
                b"",
                str(error),
            )
            raise CommandNotFound(_failure_message("Command could not be started", result, redactor), result) from None

        result = _result(
            redactor,
            validated_argv,
            cwd_text,
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )
        if check and result.returncode != 0:
            raise CommandFailed(_failure_message("Command failed", result, redactor), result)
        return result


class _Redactor:
    def __init__(self, secrets: Sequence[str]) -> None:
        invalid = [secret for secret in secrets if not isinstance(secret, str)]
        if invalid:
            raise TypeError("secrets must contain only strings")
        self._secrets = tuple(sorted({secret for secret in secrets if secret}, key=len, reverse=True))

    def __call__(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "[REDACTED]")
        return value


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not argv:
        raise ValueError("argv must be a non-empty sequence of strings")
    if any(not isinstance(argument, str) for argument in argv):
        raise ValueError("argv must contain only strings")
    if any("\0" in argument for argument in argv):
        raise ValueError("argv must not contain NUL")
    return tuple(argv)


def _build_environment(overrides: Mapping[str, str] | None) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key in _INHERITED_ENVIRONMENT_KEYS or key.startswith("LC_")
    }
    if overrides is None:
        return environment

    for key, value in overrides.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("environment overrides must map strings to strings")
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("environment overrides contain an invalid key or value")
        environment[key] = value
    return environment


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _result(
    redactor: _Redactor,
    argv: tuple[str, ...],
    cwd: str,
    returncode: int,
    stdout: bytes | str | None,
    stderr: bytes | str | None,
) -> CommandResult:
    return CommandResult(
        argv=tuple(redactor(argument) for argument in argv),
        cwd=redactor(cwd),
        returncode=returncode,
        stdout=redactor(_decode_output(stdout)),
        stderr=redactor(_decode_output(stderr)),
    )


def _failure_message(prefix: str, result: CommandResult, redactor: _Redactor) -> str:
    executable = result.argv[0] if result.argv else "<unknown>"
    return redactor(f"{prefix}: {executable!r} in {result.cwd!r} (exit {result.returncode})")


__all__ = [
    "CommandFailed",
    "CommandNotFound",
    "CommandResult",
    "CommandTimedOut",
    "ProcessRunner",
]
