from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from powercontext_eval.errors import GitSourceError
from powercontext_eval.git_source import GitSource, ResolvedGitSource
from powercontext_eval.models import PowerContextRef
from powercontext_eval.process import CommandResult, ProcessRunner

from .helpers import GitFixture, create_git_fixture, git


@pytest.fixture
def git_fixture(tmp_path: Path) -> GitFixture:
    return create_git_fixture(tmp_path)


@pytest.fixture
def source(tmp_path: Path) -> GitSource:
    return GitSource(cache_root=tmp_path / "cache")


@pytest.mark.parametrize(
    ("requested", "expected_attribute"),
    [
        (PowerContextRef(kind="branch", value="feature"), "feature_sha"),
        (PowerContextRef(kind="tag", value="v1"), "initial_sha"),
        (PowerContextRef(kind="tag", value="annotated-v1"), "initial_sha"),
    ],
)
def test_resolve_exact_branch_and_tags(
    source: GitSource,
    git_fixture: GitFixture,
    requested: PowerContextRef,
    expected_attribute: str,
) -> None:
    resolved = source.resolve(git_fixture.remote, requested)

    assert resolved.source == str(git_fixture.remote.resolve())
    assert resolved.requested == requested
    assert resolved.sha == getattr(git_fixture, expected_attribute)
    assert re.fullmatch(r"[0-9a-f]{40}", resolved.sha)
    assert resolved.cache_path.parent.name == "cache"
    assert resolved.cache_path.is_dir()


def test_resolve_full_commit_and_lowercases_sha(source: GitSource, git_fixture: GitFixture) -> None:
    requested = PowerContextRef(kind="commit", value=git_fixture.feature_sha.upper())

    resolved = source.resolve(git_fixture.remote, requested)

    assert resolved.sha == git_fixture.feature_sha


def test_resolve_rejects_exact_tag_that_points_to_blob(source: GitSource, git_fixture: GitFixture) -> None:
    blob_sha = git(git_fixture.work, "hash-object", "README.md").stdout.strip()
    git(git_fixture.work, "update-ref", "refs/tags/blob-only", blob_sha)
    git(git_fixture.work, "push", "origin", "refs/tags/blob-only")

    with pytest.raises(GitSourceError, match="could not resolve to a commit"):
        source.resolve(git_fixture.remote, PowerContextRef(kind="tag", value="blob-only"))


def test_resolve_latest_uses_clean_local_head(source: GitSource, git_fixture: GitFixture) -> None:
    resolved = source.resolve(git_fixture.work, PowerContextRef(kind="latest"))

    assert resolved.sha == git_fixture.initial_sha


def test_resolve_latest_uses_head_of_local_bare_remote(source: GitSource, git_fixture: GitFixture) -> None:
    resolved = source.resolve(git_fixture.remote, PowerContextRef(kind="latest"))

    assert resolved.sha == git_fixture.initial_sha


def test_resolve_latest_rejects_dirty_local_checkout(source: GitSource, git_fixture: GitFixture) -> None:
    (git_fixture.work / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(GitSourceError, match="clean"):
        source.resolve(git_fixture.work, PowerContextRef(kind="latest"))


def test_materialize_same_resolution_twice_at_identical_head(
    source: GitSource,
    git_fixture: GitFixture,
    tmp_path: Path,
) -> None:
    resolved = source.resolve(git_fixture.remote, PowerContextRef(kind="branch", value="feature"))

    first = source.materialize(resolved, tmp_path / "first")
    second = source.materialize(resolved, tmp_path / "second")

    assert first == tmp_path / "first"
    assert second == tmp_path / "second"
    assert git(first, "rev-parse", "HEAD").stdout.strip() == resolved.sha
    assert git(second, "rev-parse", "HEAD").stdout.strip() == resolved.sha


def test_materialize_does_not_reresolve_branch_that_moved_after_resolution(
    source: GitSource,
    git_fixture: GitFixture,
    tmp_path: Path,
) -> None:
    resolved = source.resolve(git_fixture.remote, PowerContextRef(kind="branch", value="feature"))
    moved_sha = git_fixture.commit_to_feature("move feature")
    assert moved_sha != resolved.sha

    target = source.materialize(resolved, tmp_path / "materialized")

    assert git(target, "rev-parse", "HEAD").stdout.strip() == resolved.sha


def test_resolve_refreshes_an_existing_mirror(source: GitSource, git_fixture: GitFixture) -> None:
    original = source.resolve(git_fixture.remote, PowerContextRef(kind="branch", value="feature"))
    moved_sha = git_fixture.commit_to_feature("refresh feature")

    refreshed = source.resolve(git_fixture.remote, PowerContextRef(kind="branch", value="feature"))

    assert original.sha != moved_sha
    assert refreshed.sha == moved_sha
    assert refreshed.cache_path == original.cache_path


def test_resolve_prunes_deleted_refs_from_existing_mirror(source: GitSource, git_fixture: GitFixture) -> None:
    source.resolve(git_fixture.remote, PowerContextRef(kind="branch", value="release"))
    git(git_fixture.work, "push", "origin", ":release")

    with pytest.raises(GitSourceError, match="could not resolve"):
        source.resolve(git_fixture.remote, PowerContextRef(kind="branch", value="release"))


def test_resolve_rejects_nonbare_existing_cache(source: GitSource, git_fixture: GitFixture) -> None:
    cache_path = source.cache_path_for(git_fixture.remote)
    cache_path.mkdir(parents=True)
    git(cache_path, "init")

    with pytest.raises(GitSourceError, match="not a bare mirror"):
        source.resolve(git_fixture.remote, PowerContextRef(kind="branch", value="main"))


def test_materialize_requires_nonexistent_target(
    source: GitSource,
    git_fixture: GitFixture,
    tmp_path: Path,
) -> None:
    resolved = source.resolve(git_fixture.remote, PowerContextRef(kind="branch", value="feature"))
    target = tmp_path / "exists"
    target.mkdir()

    with pytest.raises(GitSourceError, match="must not exist"):
        source.materialize(resolved, target)


def test_materialize_rejects_broken_symlink_target(
    source: GitSource,
    git_fixture: GitFixture,
    tmp_path: Path,
) -> None:
    resolved = source.resolve(git_fixture.remote, PowerContextRef(kind="branch", value="feature"))
    destination = tmp_path / "must-remain-missing"
    target = tmp_path / "broken-link"
    target.symlink_to(destination)

    with pytest.raises(GitSourceError, match="must not exist"):
        source.materialize(resolved, target)

    assert not destination.exists()


def test_resolve_missing_exact_ref_raises_typed_error(source: GitSource, git_fixture: GitFixture) -> None:
    with pytest.raises(GitSourceError, match="could not resolve"):
        source.resolve(git_fixture.remote, PowerContextRef(kind="branch", value="missing"))


def test_resolve_does_not_interpret_revision_syntax_as_part_of_exact_ref(
    source: GitSource,
    git_fixture: GitFixture,
) -> None:
    with pytest.raises(GitSourceError, match="could not resolve"):
        source.resolve(git_fixture.remote, PowerContextRef(kind="branch", value="feature^{commit}"))


class FailIfProcessRuns(ProcessRunner):
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
        raise AssertionError("normalization and cache key calculation must not access the network")


@pytest.mark.parametrize(
    ("raw_source", "normalized"),
    [
        (
            "https://user:super-secret-token@example.com/org/repo.git?access_token=query-secret#fragment-secret",
            "https://example.com/org/repo.git",
        ),
        (
            "ssh://deploy:private-key@example.com:2222/org/repo.git?token=query-secret#fragment-secret",
            "ssh://example.com:2222/org/repo.git",
        ),
        ("git@example.com:org/repo.git", "example.com:org/repo.git"),
        ("example.com:org/repo.git", "example.com:org/repo.git"),
    ],
)
def test_credential_url_normalization_and_cache_path_never_leak_secrets(
    tmp_path: Path,
    raw_source: str,
    normalized: str,
) -> None:
    source = GitSource(cache_root=tmp_path / "cache", runner=FailIfProcessRuns())

    actual_normalized = source.normalize_source(raw_source)
    cache_path = source.cache_path_for(raw_source)

    assert actual_normalized == normalized
    rendered = f"{actual_normalized}\n{cache_path}"
    for secret in ("user", "deploy", "super-secret-token", "private-key", "query-secret", "fragment-secret"):
        assert secret not in rendered
    assert re.fullmatch(r"[0-9a-f]{64}", cache_path.name)


def test_local_source_normalizes_to_absolute_resolved_path(tmp_path: Path) -> None:
    relative = Path("evaluation")
    source = GitSource(cache_root=tmp_path / "cache", runner=FailIfProcessRuns())

    assert source.normalize_source(relative) == str(relative.resolve())


def test_scp_style_source_sanitization_and_cache_key_do_not_run_process(tmp_path: Path) -> None:
    raw_source = "private-token@example.com:org/repo.git"
    source = GitSource(cache_root=tmp_path / "cache", runner=FailIfProcessRuns())

    normalized = source.normalize_source(raw_source)
    cache_path = source.cache_path_for(raw_source)
    anonymous_cache_path = source.cache_path_for("example.com:org/repo.git")

    assert normalized == "example.com:org/repo.git"
    assert cache_path == anonymous_cache_path
    rendered = f"{normalized}\n{cache_path}"
    assert "private-token" not in rendered


def test_resolved_git_source_rejects_noncanonical_sha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="40 lowercase"):
        ResolvedGitSource(
            source="/source",
            requested=PowerContextRef(kind="latest"),
            sha="ABC",
            cache_path=tmp_path / "cache",
        )
