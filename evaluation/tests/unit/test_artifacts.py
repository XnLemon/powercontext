from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from powercontext_eval.artifacts import (
    ArmState,
    ArmStateMachine,
    ArtifactStore,
    InvalidStateTransition,
    SecretDetected,
    UnsafeArtifactPath,
)


def test_write_json_is_canonical_and_replaces_existing_file(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    store.write_json("state/manifest.json", {"z": [2, 1], "a": "雪"})
    assert (tmp_path / "artifacts/state/manifest.json").read_bytes() == (
        b'{\n  "a": "\xe9\x9b\xaa",\n  "z": [\n    2,\n    1\n  ]\n}\n'
    )

    store.write_json("state/manifest.json", {"updated": True})
    assert (tmp_path / "artifacts/state/manifest.json").read_bytes() == b'{\n  "updated": true\n}\n'


def test_text_and_bytes_writes_are_exact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    store.write_text("text.txt", "hello\n")
    store.write_bytes("data.bin", b"\x00\xff")
    assert (tmp_path / "artifacts/text.txt").read_bytes() == b"hello\n"
    assert (tmp_path / "artifacts/data.bin").read_bytes() == b"\x00\xff"


def test_json_rejects_non_standard_non_finite_numbers_without_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ValueError):
        store.write_json("invalid.json", {"value": math.nan})
    assert not (tmp_path / "artifacts/invalid.json").exists()


@pytest.mark.parametrize("path", ["", ".", "..", "../escape", "a/../../escape", "/absolute", "a/\x00/b"])
def test_unsafe_relative_paths_are_rejected(tmp_path: Path, path: str) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(UnsafeArtifactPath):
        store.write_text(path, "data")
    assert not (tmp_path / "escape").exists()


def test_parent_and_final_symlinks_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked-parent").symlink_to(outside, target_is_directory=True)
    (root / "linked-file").symlink_to(outside / "file")
    store = ArtifactStore(root)

    with pytest.raises(UnsafeArtifactPath):
        store.write_text("linked-parent/escape.txt", "data")
    with pytest.raises(UnsafeArtifactPath):
        store.write_text("linked-file", "data")
    assert not (outside / "escape.txt").exists()
    assert not (outside / "file").exists()


def test_symlink_root_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(UnsafeArtifactPath):
        ArtifactStore(linked)


@pytest.mark.parametrize("secret", ["", b""])
def test_empty_forbidden_values_are_rejected(tmp_path: Path, secret: str | bytes) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ArtifactStore(tmp_path / "artifacts", forbidden_values=[secret])


@pytest.mark.parametrize("secret", ["super-secret", b"binary-secret"])
def test_secret_is_rejected_without_target_temp_or_message_leak(tmp_path: Path, secret: str | bytes) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root, forbidden_values=[secret])
    payload = secret.encode() if isinstance(secret, str) else secret

    with pytest.raises(SecretDetected) as caught:
        store.write_bytes("nested/result.txt", b"prefix-" + payload + b"-suffix")

    assert payload.decode() not in str(caught.value)
    assert not (root / "nested/result.txt").exists()
    assert not (root / "nested").exists()


def test_failed_atomic_replace_preserves_old_target_and_cleans_own_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    store.write_text("state.json", "old")

    def fail_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        store.write_text("state.json", "new")

    assert (root / "state.json").read_text() == "old"
    assert [path.name for path in root.iterdir()] == ["state.json"]


LEGAL_TRANSITIONS = {
    ArmState.CREATED: (ArmState.REVISIONS_RESOLVED, ArmState.CONFIGURATION_ERROR),
    ArmState.REVISIONS_RESOLVED: (
        ArmState.GOLD_VERIFIED,
        ArmState.GOLD_CHECK_FAILED,
        ArmState.INFRASTRUCTURE_ERROR,
    ),
    ArmState.GOLD_VERIFIED: (ArmState.ENVIRONMENT_READY, ArmState.INFRASTRUCTURE_ERROR),
    ArmState.ENVIRONMENT_READY: (ArmState.CODEX_RUNNING, ArmState.INFRASTRUCTURE_ERROR),
    ArmState.CODEX_RUNNING: (ArmState.PATCH_CAPTURED, ArmState.CODEX_ERROR, ArmState.CODEX_TIMEOUT),
    ArmState.PATCH_CAPTURED: (ArmState.EVALUATED, ArmState.EVALUATION_ERROR),
    ArmState.EVALUATED: (ArmState.TREATMENT_VALIDATED, ArmState.INVALID_TREATMENT),
    ArmState.TREATMENT_VALIDATED: (ArmState.REPORTED,),
}


@pytest.mark.parametrize(("source", "targets"), LEGAL_TRANSITIONS.items())
def test_every_legal_state_transition(tmp_path: Path, source: ArmState, targets: tuple[ArmState, ...]) -> None:
    for target in targets:
        arm_root = tmp_path / f"{source}-{target}"
        store = ArtifactStore(arm_root)
        machine = ArmStateMachine(store, "off", initial_state=source, initial_sequence=7)
        snapshot = machine.transition(target, {"sha": "a" * 40})
        assert snapshot.state is target
        assert snapshot.sequence == 8
        assert json.loads((arm_root / "state.json").read_text()) == {
            "arm": "off",
            "evidence": {"sha": "a" * 40},
            "sequence": 8,
            "state": target.value,
        }


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (ArmState.CREATED, ArmState.CREATED),
        (ArmState.CREATED, ArmState.CODEX_RUNNING),
        (ArmState.CODEX_RUNNING, ArmState.ENVIRONMENT_READY),
        (ArmState.REPORTED, ArmState.CREATED),
        (ArmState.CODEX_ERROR, ArmState.REPORTED),
    ],
)
def test_invalid_transition_preserves_memory_and_disk(tmp_path: Path, source: ArmState, target: ArmState) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    machine = ArmStateMachine(store, "on", initial_state=source, initial_sequence=3)
    before = (tmp_path / "artifacts/state.json").read_bytes()

    with pytest.raises(InvalidStateTransition):
        machine.transition(target, {"reason": "must not persist"})

    assert machine.state is source
    assert machine.sequence == 3
    assert (tmp_path / "artifacts/state.json").read_bytes() == before


def test_unknown_transition_is_typed_and_does_not_change_state(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    machine = ArmStateMachine(store, "off")
    before = (tmp_path / "artifacts/state.json").read_bytes()
    with pytest.raises(InvalidStateTransition):
        machine.transition("invented", {})  # type: ignore[arg-type]
    assert machine.state is ArmState.CREATED
    assert (tmp_path / "artifacts/state.json").read_bytes() == before
