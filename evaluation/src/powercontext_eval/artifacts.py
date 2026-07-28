"""Atomic artifact persistence and arm lifecycle state."""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath
from types import MappingProxyType
from typing import Any

from powercontext_eval.models import Arm


class ArtifactError(Exception):
    """Base class for artifact persistence errors."""


class UnsafeArtifactPath(ArtifactError):
    """An artifact path could escape or traverse a symbolic link."""


class SecretDetected(ArtifactError):
    """Artifact bytes contain a configured forbidden value."""


class ArtifactAlreadyExists(ArtifactError):
    """An exclusive artifact target already exists."""


class ArtifactDurabilityUnknown(ArtifactError):
    """A commit-point failure left an artifact's durable state unknown."""

    def __init__(self, target_name: str, recovery_name: str | None = None) -> None:
        super().__init__("Artifact durability is unknown; inspect typed recovery metadata")
        self.target_name = target_name
        self.recovery_name = recovery_name


class StateAlreadyExists(ArtifactAlreadyExists):
    """An arm state file already exists and must not be overwritten."""


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
    """Write artifacts through directory descriptors anchored at the filesystem root."""

    def __init__(self, root: str | os.PathLike[str], *, forbidden_values: Sequence[str | bytes] = ()) -> None:
        self.root = Path(root).absolute()
        if "\x00" in os.fspath(root) or ".." in self.root.parts:
            raise UnsafeArtifactPath("Artifact root contains an unsafe component")
        self._forbidden = tuple(self._encode_forbidden(value) for value in forbidden_values)
        root_fd = self._open_root(create=True)
        os.close(root_fd)

    @staticmethod
    def _encode_forbidden(value: str | bytes) -> bytes:
        encoded = value.encode("utf-8") if isinstance(value, str) else value
        if not encoded:
            raise ValueError("Forbidden values must be non-empty")
        return encoded

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    @classmethod
    def _open_directory(cls, parent_fd: int, name: str, *, create: bool) -> int:
        try:
            return os.open(name, cls._directory_flags(), dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
            try:
                return os.open(name, cls._directory_flags(), dir_fd=parent_fd)
            except OSError as error:
                raise UnsafeArtifactPath("Artifact directory is unsafe") from error
        except OSError as error:
            raise UnsafeArtifactPath("Artifact directory is unsafe") from error

    def _open_root(self, *, create: bool) -> int:
        anchor_fd = os.open(self.root.anchor, self._directory_flags())
        current_fd = anchor_fd
        try:
            for component in self.root.parts[1:]:
                next_fd = self._open_directory(current_fd, component, create=create)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

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

    def _open_parent(self, relative_path: str | os.PathLike[str], *, create: bool) -> tuple[int, tuple[str, ...]]:
        parts = self._validate_relative(relative_path)
        current_fd = self._open_root(create=create)
        try:
            for component in parts[:-1]:
                next_fd = self._open_directory(current_fd, component, create=create)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd, parts
        except BaseException:
            os.close(current_fd)
            raise

    def _verify_logical_parent(self, parts: tuple[str, ...], parent_fd: int) -> None:
        """Rewalk the logical path and require it to identify the held parent."""

        logical_fd = self._open_root(create=False)
        try:
            for component in parts[:-1]:
                next_fd = self._open_directory(logical_fd, component, create=False)
                os.close(logical_fd)
                logical_fd = next_fd
            held = os.fstat(parent_fd)
            logical = os.fstat(logical_fd)
            if (held.st_dev, held.st_ino) != (logical.st_dev, logical.st_ino):
                raise UnsafeArtifactPath("Artifact parent identity changed")
        except (FileNotFoundError, UnsafeArtifactPath) as error:
            raise UnsafeArtifactPath("Artifact logical parent is no longer safe") from error
        finally:
            os.close(logical_fd)

    @staticmethod
    def _target_metadata(parent_fd: int, name: str) -> os.stat_result | None:
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise UnsafeArtifactPath("Artifact target is not a regular file")
        return metadata

    def _reject_secrets(self, data: bytes) -> None:
        if any(secret in data for secret in self._forbidden):
            raise SecretDetected("Artifact contains a forbidden value")

    @staticmethod
    def _random_name(target_name: str, kind: str) -> str:
        return f".{target_name}.{kind}-{secrets.token_hex(16)}"

    @classmethod
    def _create_temp(cls, parent_fd: int, target_name: str) -> tuple[int, str]:
        for _ in range(32):
            name = cls._random_name(target_name, "tmp")
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
                return descriptor, name
            except FileExistsError:
                continue
        raise ArtifactError("Could not allocate an artifact temporary file")

    @staticmethod
    def _write_all(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("Artifact temporary write made no progress")
            written += count
        os.fsync(descriptor)

    @staticmethod
    def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
        return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)

    @staticmethod
    def _name_exists(parent_fd: int, name: str | None) -> bool:
        if name is None:
            return False
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def _assert_temp_named(self, parent_fd: int, temporary_name: str, temporary_fd: int) -> None:
        expected = os.fstat(temporary_fd)
        try:
            named = os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as error:
            raise UnsafeArtifactPath("Artifact temporary file moved") from error
        if not stat.S_ISREG(named.st_mode) or not self._same_inode(expected, named):
            raise UnsafeArtifactPath("Artifact temporary file identity changed")

    @classmethod
    def _unlink_inode(cls, parent_fd: int, preferred_name: str, expected: os.stat_result) -> bool:
        names = [preferred_name]
        try:
            names.extend(name for name in os.listdir(parent_fd) if name != preferred_name)
        except OSError:
            pass
        for name in names:
            try:
                candidate = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(candidate.st_mode) and cls._same_inode(expected, candidate):
                try:
                    os.unlink(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except FileNotFoundError:
                    continue
                return True
        return False

    @classmethod
    def _unlink_name_if_inode(cls, parent_fd: int, name: str, expected: os.stat_result) -> bool:
        try:
            candidate = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not cls._same_inode(expected, candidate):
            return False
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True

    @classmethod
    def _create_backup(cls, parent_fd: int, target_name: str) -> str:
        for _ in range(32):
            backup_name = cls._random_name(target_name, "backup")
            try:
                os.link(
                    target_name,
                    backup_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                return backup_name
            except FileExistsError:
                continue
        raise ArtifactError("Could not allocate an artifact rollback link")

    def _publish(
        self,
        parent_fd: int,
        parts: tuple[str, ...],
        temporary_fd: int,
        temporary_name: str,
        *,
        exclusive: bool,
    ) -> None:
        target_name = parts[-1]
        temporary_metadata = os.fstat(temporary_fd)
        backup_name: str | None = None
        published = False
        try:
            target_metadata = self._target_metadata(parent_fd, target_name)
            if exclusive and target_metadata is not None:
                raise ArtifactAlreadyExists("Artifact already exists")
            if not exclusive and target_metadata is not None:
                backup_name = self._create_backup(parent_fd, target_name)
                os.fsync(parent_fd)

            self._verify_logical_parent(parts, parent_fd)
            self._assert_temp_named(parent_fd, temporary_name, temporary_fd)
            if exclusive:
                try:
                    os.link(
                        temporary_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as error:
                    raise ArtifactAlreadyExists("Artifact already exists") from error
            else:
                os.replace(
                    temporary_name,
                    target_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            published = True
            os.fsync(parent_fd)
            published_metadata = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
            if not self._same_inode(temporary_metadata, published_metadata):
                raise UnsafeArtifactPath("Published artifact identity changed")
            self._verify_logical_parent(parts, parent_fd)
        except BaseException as publish_error:
            if published:
                try:
                    if backup_name is not None:
                        os.replace(backup_name, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                        os.fsync(parent_fd)
                        backup_name = None
                    else:
                        self._unlink_name_if_inode(parent_fd, target_name, temporary_metadata)
                except (ArtifactError, OSError):
                    raise ArtifactDurabilityUnknown(target_name, backup_name) from publish_error
            try:
                if backup_name is not None:
                    os.unlink(backup_name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                self._unlink_inode(parent_fd, temporary_name, temporary_metadata)
            except (ArtifactError, OSError):
                raise ArtifactDurabilityUnknown(target_name, backup_name) from publish_error
            raise
        else:
            try:
                if backup_name is not None:
                    os.unlink(backup_name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                if exclusive:
                    self._unlink_inode(parent_fd, temporary_name, temporary_metadata)
            except BaseException as cleanup_error:
                recovery_name = backup_name if self._name_exists(parent_fd, backup_name) else None
                if exclusive and self._name_exists(parent_fd, temporary_name):
                    recovery_name = temporary_name
                raise ArtifactDurabilityUnknown(target_name, recovery_name) from cleanup_error

    def _store_bytes(self, relative_path: str | os.PathLike[str], data: bytes, *, exclusive: bool) -> Path:
        if not isinstance(data, bytes):
            raise TypeError("Artifact data must be bytes")
        self._reject_secrets(data)
        parent_fd, parts = self._open_parent(relative_path, create=True)
        temporary_fd = -1
        temporary_name = ""
        try:
            temporary_fd, temporary_name = self._create_temp(parent_fd, parts[-1])
            self._write_all(temporary_fd, data)
            self._publish(
                parent_fd,
                parts,
                temporary_fd,
                temporary_name,
                exclusive=exclusive,
            )
        except ArtifactDurabilityUnknown:
            raise
        except BaseException:
            if temporary_fd >= 0:
                self._unlink_inode(parent_fd, temporary_name, os.fstat(temporary_fd))
            raise
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            os.close(parent_fd)
        return self.root.joinpath(*parts)

    def write_bytes(self, relative_path: str | os.PathLike[str], data: bytes) -> Path:
        """Atomically write exact bytes after path and secret validation."""

        return self._store_bytes(relative_path, data, exclusive=False)

    def create_bytes(self, relative_path: str | os.PathLike[str], data: bytes) -> Path:
        """Atomically create exact bytes without replacing an existing target."""

        return self._store_bytes(relative_path, data, exclusive=True)

    def write_text(self, relative_path: str | os.PathLike[str], text: str) -> Path:
        """Atomically write UTF-8 text exactly as supplied."""

        return self.write_bytes(relative_path, text.encode("utf-8"))

    def create_text(self, relative_path: str | os.PathLike[str], text: str) -> Path:
        """Atomically create UTF-8 text without replacing an existing target."""

        return self.create_bytes(relative_path, text.encode("utf-8"))

    def write_json(self, relative_path: str | os.PathLike[str], value: Any) -> Path:
        """Atomically write canonical, human-readable UTF-8 JSON."""

        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
            "utf-8"
        )
        return self.write_bytes(relative_path, encoded)

    def create_json(self, relative_path: str | os.PathLike[str], value: Any) -> Path:
        """Atomically create canonical JSON without replacing an existing target."""

        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(
            "utf-8"
        )
        return self.create_bytes(relative_path, encoded)


def _require_string_mapping_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            _require_string_mapping_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _require_string_mapping_keys(nested)


def _canonical_json_value(value: Any) -> Any:
    _require_string_mapping_keys(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return json.loads(encoded)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(nested) for key, nested in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(nested) for nested in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(nested) for nested in value]
    return value


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
            "evidence": _thaw_json(self.evidence),
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
        self._snapshot = ArmStateSnapshot(
            self._arm,
            self._state,
            self._sequence,
            _freeze_json(_canonical_json_value({})),
        )
        try:
            self._store.create_json(self._state_path, self._snapshot.as_json())
        except ArtifactAlreadyExists as error:
            raise StateAlreadyExists("Arm state already exists") from error

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

        frozen_evidence = _freeze_json(_canonical_json_value(evidence))
        snapshot = ArmStateSnapshot(self._arm, parsed_target, self._sequence + 1, frozen_evidence)
        self._store.write_json(self._state_path, snapshot.as_json())
        self._state = parsed_target
        self._sequence = snapshot.sequence
        self._snapshot = snapshot
        return snapshot
