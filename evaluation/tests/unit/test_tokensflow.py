from __future__ import annotations

import os
import socket
import stat
import tempfile
from pathlib import Path

import pytest

from powercontext_eval.tokensflow import (
    UnsafeTokensFlowConfiguration,
    snapshot_tokensflow_home,
    tokensflow_secret_variants,
)


def _profile(tmp_path: Path, credentials: str = '{"access":"first"}') -> Path:
    user_home = tmp_path / "profile"
    config = user_home / ".tokensflow"
    config.mkdir(parents=True, mode=0o700)
    (config / "credentials.json").write_text(credentials)
    return user_home


def test_snapshot_tokensflow_home_is_private_and_content_current(tmp_path: Path) -> None:
    source_home = _profile(tmp_path)
    config = source_home / ".tokensflow"
    (config / "config.toml").write_text('endpoint = "current"\n')
    nested = config / "profiles" / "active"
    nested.mkdir(parents=True)
    (nested / "settings.json").write_text('{"mode":"live"}')
    destination = tmp_path / "arm/runtime/tokensflow-home"

    snapshot = snapshot_tokensflow_home(source_home, destination)

    assert snapshot.user_home == destination
    assert snapshot.credentials == destination / ".tokensflow/credentials.json"
    assert snapshot.credentials.read_text() == '{"access":"first"}'
    assert (destination / ".tokensflow/config.toml").read_text() == 'endpoint = "current"\n'
    assert (destination / ".tokensflow/profiles/active/settings.json").read_text() == '{"mode":"live"}'
    assert (destination / ".local/share/tokensflow").is_dir()
    for directory in (
        destination,
        destination / ".tokensflow",
        destination / ".tokensflow/profiles",
        destination / ".tokensflow/profiles/active",
        destination / ".local",
        destination / ".local/share",
        destination / ".local/share/tokensflow",
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for file_path in (
        snapshot.credentials,
        destination / ".tokensflow/config.toml",
        destination / ".tokensflow/profiles/active/settings.json",
    ):
        assert stat.S_IMODE(file_path.stat().st_mode) == 0o600


def test_snapshot_uses_current_content_for_each_new_arm(tmp_path: Path) -> None:
    source_home = _profile(tmp_path)

    first = snapshot_tokensflow_home(source_home, tmp_path / "first")
    (source_home / ".tokensflow/credentials.json").write_text('{"access":"second"}')
    second = snapshot_tokensflow_home(source_home, tmp_path / "second")

    assert first.credentials.read_text() == '{"access":"first"}'
    assert second.credentials.read_text() == '{"access":"second"}'


def test_snapshot_allows_missing_optional_config_toml(tmp_path: Path) -> None:
    destination = tmp_path / "destination"

    snapshot_tokensflow_home(_profile(tmp_path), destination)

    assert not (destination / ".tokensflow/config.toml").exists()


@pytest.mark.parametrize("missing", ["profile", "config", "credentials"])
def test_snapshot_rejects_missing_required_source(tmp_path: Path, missing: str) -> None:
    source_home = tmp_path / "profile"
    if missing in {"config", "credentials"}:
        source_home.mkdir()
    if missing == "credentials":
        (source_home / ".tokensflow").mkdir()

    with pytest.raises(UnsafeTokensFlowConfiguration, match="safe TokensFlow profile"):
        snapshot_tokensflow_home(source_home, tmp_path / "destination")


@pytest.mark.parametrize("linked_component", ["home", "config", "file"])
def test_snapshot_rejects_symlinks(tmp_path: Path, linked_component: str) -> None:
    real_home = _profile(tmp_path)
    source_home = real_home
    if linked_component == "home":
        source_home = tmp_path / "linked-home"
        source_home.symlink_to(real_home, target_is_directory=True)
    elif linked_component == "config":
        source_home = tmp_path / "wrapper"
        source_home.mkdir()
        (source_home / ".tokensflow").symlink_to(real_home / ".tokensflow", target_is_directory=True)
    else:
        target = tmp_path / "external.json"
        target.write_text("{}")
        credentials = real_home / ".tokensflow/credentials.json"
        credentials.unlink()
        credentials.symlink_to(target)

    with pytest.raises(UnsafeTokensFlowConfiguration, match="safe TokensFlow profile"):
        snapshot_tokensflow_home(source_home, tmp_path / "destination")


@pytest.mark.parametrize("entry_kind", ["fifo", "socket"])
def test_snapshot_rejects_special_entries(tmp_path: Path, entry_kind: str) -> None:
    with tempfile.TemporaryDirectory(prefix="tokensflow-test-", dir="/tmp") as temporary:
        short_root = Path(temporary)
        source_home = _profile(short_root)
        special = source_home / ".tokensflow" / entry_kind
        listener: socket.socket | None = None
        if entry_kind == "fifo":
            os.mkfifo(special)
        else:
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(os.fspath(special))
        try:
            with pytest.raises(UnsafeTokensFlowConfiguration, match="safe TokensFlow profile"):
                snapshot_tokensflow_home(source_home, short_root / "destination")
        finally:
            if listener is not None:
                listener.close()


def test_snapshot_rejects_lexical_path_escape(tmp_path: Path) -> None:
    source_home = _profile(tmp_path)
    escaped_destination = tmp_path / "arm" / ".." / "escaped"

    with pytest.raises(UnsafeTokensFlowConfiguration, match="safe TokensFlow profile"):
        snapshot_tokensflow_home(source_home, escaped_destination)


def test_snapshot_rejects_preexisting_destination(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()

    with pytest.raises(UnsafeTokensFlowConfiguration, match="safe TokensFlow profile"):
        snapshot_tokensflow_home(_profile(tmp_path), destination)


def test_tokensflow_secret_variants_only_expand_sensitive_string_fields(tmp_path: Path) -> None:
    source_home = _profile(
        tmp_path,
        (
            '{"access_token":"long-access-secret","enabled":true,"expires_in":3600,'
            '"token_type":"Bearer","nested":{"refresh_token":"secret/value"}}'
        ),
    )

    variants = tokensflow_secret_variants(source_home / ".tokensflow/credentials.json")

    assert "long-access-secret" in variants
    assert "secret/value" in variants
    assert "secret%2Fvalue" in variants
    assert "true" not in variants
    assert "3600" not in variants
    assert "Bearer" not in variants
