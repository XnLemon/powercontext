from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

from powercontext_eval.process import (
    CommandFailed,
    CommandNotFound,
    CommandResult,
    CommandTimedOut,
    ProcessRunner,
)


def test_run_captures_utf8_output_and_replaces_invalid_bytes(tmp_path: Path) -> None:
    result = ProcessRunner().run(
        [
            sys.executable,
            "-c",
            "import os, sys; print(os.getcwd(), flush=True); sys.stdout.buffer.write(b'\\xff\\n'); print('err', file=sys.stderr)",
        ],
        cwd=tmp_path,
    )

    assert result == CommandResult(
        argv=(
            sys.executable,
            "-c",
            "import os, sys; print(os.getcwd(), flush=True); sys.stdout.buffer.write(b'\\xff\\n'); print('err', file=sys.stderr)",
        ),
        cwd=str(tmp_path),
        returncode=0,
        stdout=f"{tmp_path}\n\ufffd\n",
        stderr="err\n",
    )


def test_command_result_is_frozen() -> None:
    result = CommandResult(argv=("true",), cwd="/tmp", returncode=0, stdout="", stderr="")
    field_name = "returncode"

    with pytest.raises(FrozenInstanceError):
        setattr(result, field_name, 1)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        [sys.executable, 1],
        [sys.executable, "bad\0argument"],
    ],
)
def test_run_rejects_invalid_argv(argv: list[object], tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ProcessRunner().run(cast(Sequence[str], argv), cwd=tmp_path)


def test_run_raises_command_failed_with_result_for_nonzero_exit(tmp_path: Path) -> None:
    with pytest.raises(CommandFailed) as captured:
        ProcessRunner().run(
            [sys.executable, "-c", "import sys; print('nope', file=sys.stderr); raise SystemExit(7)"],
            cwd=tmp_path,
        )

    assert captured.value.result.returncode == 7
    assert captured.value.result.stderr == "nope\n"


def test_run_can_return_nonzero_result_when_check_is_false(tmp_path: Path) -> None:
    result = ProcessRunner().run(
        [sys.executable, "-c", "raise SystemExit(9)"],
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 9


def test_run_raises_command_timed_out_with_partial_result(tmp_path: Path) -> None:
    with pytest.raises(CommandTimedOut) as captured:
        ProcessRunner().run(
            [
                sys.executable,
                "-c",
                "import sys, time; print('started', flush=True); print('waiting', file=sys.stderr, flush=True); time.sleep(5)",
            ],
            cwd=tmp_path,
            timeout=0.1,
        )

    assert captured.value.result.returncode == 124
    assert captured.value.result.stdout == "started\n"
    assert captured.value.result.stderr == "waiting\n"


def test_run_raises_command_not_found_with_result(tmp_path: Path) -> None:
    missing = "powercontext-command-that-does-not-exist"

    with pytest.raises(CommandNotFound) as captured:
        ProcessRunner().run([missing], cwd=tmp_path)

    assert captured.value.result.returncode == 127
    assert captured.value.result.argv == (missing,)


def test_run_redacts_secrets_from_result_and_error_message(tmp_path: Path) -> None:
    short_secret = "token"
    long_secret = "token-with-detail"
    secret_cwd = tmp_path / f"workspace-{long_secret}"
    secret_cwd.mkdir()
    script = "import sys; print(sys.argv[1], file=sys.stderr); raise SystemExit(3)"

    with pytest.raises(CommandFailed) as captured:
        ProcessRunner().run(
            [sys.executable, "-c", script, long_secret],
            cwd=secret_cwd,
            secrets=["", short_secret, long_secret],
        )

    error = captured.value
    rendered = "\n".join(
        [
            str(error),
            repr(error),
            repr(error.result),
            error.result.stdout,
            error.result.stderr,
            *error.result.argv,
            error.result.cwd,
        ]
    )
    assert short_secret not in rendered
    assert long_secret not in rendered
    assert "[REDACTED]-with-detail" not in rendered
    assert error.result.argv[-1] == "[REDACTED]"
    assert error.result.stderr == "[REDACTED]\n"
    assert error.result.cwd.endswith("workspace-[REDACTED]")


def test_run_redacts_secret_from_not_found_exception(tmp_path: Path) -> None:
    secret = "sensitive-command"

    with pytest.raises(CommandNotFound) as captured:
        ProcessRunner().run([secret], cwd=tmp_path, secrets=[secret])

    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert captured.value.result.argv == ("[REDACTED]",)


def test_run_inherits_only_allowlisted_environment_and_allows_explicit_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POWERCONTEXT_UNRELATED_SECRET", "must-not-leak")
    monkeypatch.setenv("HTTP_PROXY", "http://allowed.example")
    monkeypatch.setenv("LC_POWERCONTEXT_TEST", "allowed-locale")
    script = (
        "import os; "
        "print(os.environ.get('POWERCONTEXT_UNRELATED_SECRET', '<missing>')); "
        "print(os.environ['HTTP_PROXY']); "
        "print(os.environ['LC_POWERCONTEXT_TEST']); "
        "print(os.environ['EXPLICIT_VALUE'])"
    )

    result = ProcessRunner().run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={"EXPLICIT_VALUE": "added-by-caller"},
    )

    assert result.stdout.splitlines() == [
        "<missing>",
        "http://allowed.example",
        "allowed-locale",
        "added-by-caller",
    ]
