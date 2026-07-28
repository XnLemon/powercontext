"""Atomic artifact persistence and arm lifecycle state."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath
from typing import Any

from powercontext_eval.models import Arm


class ArtifactError(Exception):
    """Base class for artifact persistence errors."""


class UnsafeArtifactPath(ArtifactError):
    """An artifact path could escape or traverse a symbolic link."""


class SecretDetected(ArtifactError):
    """Artifact bytes contain a configured forbidden value."""


class InvalidStateTransition(ArtifactError):
    """An arm lifecycle transition is not permitted."""


class ArmState(StrEnum):
    """Durable lifecycle states for one evaluation arm."""

    CREATED = "created"
    REVISIONS_RESOLVED = "revisions_resolved"
    CONFIGURATION_ERROR = "configuration_error"
    GOLD_VERIFIED = "gold_verified"
    GOLD_CHECK_FAILED = "gold_check_failed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    ENVIRONMENT_READY = "environment_ready"
    CODEX_RUNNING = "codex_running"
    PATCH_CAPTURED = "patch_captured"
    CODEX_ERROR = "codex_error"
    CODEX_TIMEOUT = "codex_timeout"
    EVALUATED = "evaluated"
    EVALUATION_ERROR = "evaluation_error"
    TREATMENT_VALIDATED = "treatment_validated"
    INVALID_TREATMENT = "invalid_treatment"
    REPORTED = "reported"


_TRANSITIONS: Mapping[ArmState, frozenset[ArmState]] = {
    ArmState.CREATED: frozenset({ArmState.REVISIONS_RESOLVED, ArmState.CONFIGURATION_ERROR}),
    ArmState.REVISIONS_RESOLVED: frozenset(
        {ArmState.GOLD_VERIFIED, ArmState.GOLD_CHECK_FAILED, ArmState.INFRASTRUCTURE_ERROR}
    ),
    ArmState.GOLD_VERIFIED: frozenset({ArmState.ENVIRONMENT_READY, ArmState.INFRASTRUCTURE_ERROR}),
    ArmState.ENVIRONMENT_READY: frozenset({ArmState.CODEX_RUNNING, ArmState.INFRASTRUCTURE_ERROR}),
    ArmState.CODEX_RUNNING: frozenset({ArmState.PATCH_CAPTURED, ArmState.CODEX_ERROR, ArmState.CODEX_TIMEOUT}),
    ArmState.PATCH_CAPTURED: frozenset({ArmState.EVALUATED, ArmState.EVALUATION_ERROR}),
    ArmState.EVALUATED: frozenset({ArmState.TREATMENT_VALIDATED, ArmState.INVALID_TREATMENT}),
    ArmState.TREATMENT_VALIDATED: frozenset({ArmState.REPORTED}),
}


class ArtifactStore:
    """Write only explicitly supplied artifact bytes beneath a trusted root."""

    def __init__(self, root: str | os.PathLike[str], *, forbidden_values: Sequence[str | bytes] = ()) -> None:
        self.root = Path(root).absolute()
        self._forbidden = tuple(self._encode_forbidden(value) for value in forbidden_values)
        self.root.mkdir(parents=True, exist_ok=True)
        self._verify_directory(self.root)

    @staticmethod
    def _encode_forbidden(value: str | bytes) -> bytes:
        encoded = value.encode("utf-8") if isinstance(value, str) else value
        if not encoded:
            raise ValueError("Forbidden values must be non-empty")
        return encoded

    @staticmethod
    def _verify_directory(path: Path) -> None:
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            raise UnsafeArtifactPath("Artifact directory cannot be inspected") from error
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise UnsafeArtifactPath("Artifact path component is not a real directory")

    @staticmethod
    def _validate_relative(relative_path: str | os.PathLike[str]) -> tuple[str, ...]:
        raw = os.fspath(relative_path)
        if not raw or "\x00" in raw:
            raise UnsafeArtifactPath("Artifact path must be a non-empty relative path")
        raw_parts = raw.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} for part in raw_parts):
            raise UnsafeArtifactPath("Artifact path contains an unsafe component")
        pure = PurePath(raw)
        parts = pure.parts
        if pure.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
            raise UnsafeArtifactPath("Artifact path must remain beneath the artifact root")
        return parts

    def _verify_contained(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise UnsafeArtifactPath("Artifact path escaped the artifact root") from error

    def _target(self, relative_path: str | os.PathLike[str]) -> Path:
        parts = self._validate_relative(relative_path)
        self._verify_directory(self.root)
        parent = self.root
        for part in parts[:-1]:
            parent = parent / part
            self._verify_contained(parent)
            try:
                parent.mkdir()
            except FileExistsError:
                pass
            self._verify_directory(parent)

        target = parent / parts[-1]
        self._verify_contained(target)
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise UnsafeArtifactPath("Artifact target is not a regular file")
        return target

    def _reject_secrets(self, data: bytes) -> None:
        if any(secret in data for secret in self._forbidden):
            raise SecretDetected("Artifact contains a forbidden value")

    def write_bytes(self, relative_path: str | os.PathLike[str], data: bytes) -> Path:
        """Atomically write exact bytes after path and secret validation."""

        if not isinstance(data, bytes):
            raise TypeError("Artifact data must be bytes")
        self._reject_secrets(data)
        target = self._target(relative_path)
        parent = target.parent
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.tmp-", dir=parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._verify_directory(self.root)
            self._verify_directory(parent)
            self._verify_contained(target)
            try:
                target_mode = target.lstat().st_mode
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(target_mode) or not stat.S_ISREG(target_mode):
                    raise UnsafeArtifactPath("Artifact target changed during the write")
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return target

    def write_text(self, relative_path: str | os.PathLike[str], text: str) -> Path:
        """Atomically write UTF-8 text exactly as supplied."""

        return self.write_bytes(relative_path, text.encode("utf-8"))

    def write_json(self, relative_path: str | os.PathLike[str], value: Any) -> Path:
        """Atomically write canonical, human-readable UTF-8 JSON."""

        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
            "utf-8"
        )
        return self.write_bytes(relative_path, encoded)


@dataclass(frozen=True)
class ArmStateSnapshot:
    """The persisted state after one successful transition."""

    arm: Arm
    state: ArmState
    sequence: int
    evidence: Mapping[str, Any]

    def as_json(self) -> dict[str, Any]:
        """Return the canonical persistence representation."""

        return {
            "arm": self.arm.value,
            "state": self.state.value,
            "sequence": self.sequence,
            "evidence": dict(self.evidence),
        }


class ArmStateMachine:
    """Validate and durably persist monotonic arm lifecycle transitions."""

    def __init__(
        self,
        store: ArtifactStore,
        arm: Arm | str,
        *,
        state_path: str = "state.json",
        initial_state: ArmState = ArmState.CREATED,
        initial_sequence: int = 0,
    ) -> None:
        self._store = store
        self._state_path = state_path
        self._arm = Arm(arm)
        self._state = initial_state
        self._sequence = initial_sequence
        self._snapshot = ArmStateSnapshot(self._arm, self._state, self._sequence, {})
        self._store.write_json(self._state_path, self._snapshot.as_json())

    @property
    def state(self) -> ArmState:
        """Return current in-memory state."""

        return self._state

    @property
    def sequence(self) -> int:
        """Return the monotonic transition sequence."""

        return self._sequence

    def transition(self, target: ArmState | str, evidence: Mapping[str, Any]) -> ArmStateSnapshot:
        """Persist an allowed transition before updating in-memory state."""

        try:
            parsed_target = ArmState(target)
        except (TypeError, ValueError) as error:
            raise InvalidStateTransition("Unknown arm state") from error
        if parsed_target not in _TRANSITIONS.get(self._state, frozenset()):
            raise InvalidStateTransition(f"Transition from {self._state.value} to {parsed_target.value} is not allowed")

        snapshot = ArmStateSnapshot(self._arm, parsed_target, self._sequence + 1, dict(evidence))
        self._store.write_json(self._state_path, snapshot.as_json())
        self._state = parsed_target
        self._sequence = snapshot.sequence
        self._snapshot = snapshot
        return snapshot
