"""Immutable Git source resolution and materialization."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

from powercontext_eval.errors import CommandError, GitSourceError
from powercontext_eval.models import PowerContextRef
from powercontext_eval.process import CommandResult, ProcessRunner

_FULL_LOWERCASE_SHA = re.compile(r"[0-9a-f]{40}")
_SUPPORTED_URL_SCHEMES = frozenset({"http", "https", "ssh"})
_SCP_STYLE_URL = re.compile(r"^(?:(?P<user>[^/@:\s]+)@)?(?P<host>[^/:\s]+):(?P<path>.+)$")


@dataclass(frozen=True)
class ResolvedGitSource:
    """A source reference resolved once to an immutable Git commit."""

    source: str
    requested: PowerContextRef
    sha: str
    cache_path: Path

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("Resolved source must not be empty")
        if not isinstance(self.requested, PowerContextRef):
            raise TypeError("requested must be a PowerContextRef")
        if _FULL_LOWERCASE_SHA.fullmatch(self.sha) is None:
            raise ValueError("Resolved SHA must contain exactly 40 lowercase hexadecimal characters")
        if not isinstance(self.cache_path, Path):
            raise TypeError("cache_path must be a Path")


@dataclass(frozen=True)
class _SourceDetails:
    normalized: str
    transport: str
    local_path: Path | None
    secrets: tuple[str, ...]


class GitSource:
    """Resolve explicit Git refs into cached immutable commits."""

    def __init__(self, *, cache_root: str | Path, runner: ProcessRunner | None = None) -> None:
        self._cache_root = Path(cache_root).expanduser().resolve()
        self._runner = runner or ProcessRunner()

    def normalize_source(self, source: str | Path) -> str:
        """Return the credential-free source used for provenance and cache identity."""

        return _source_details(source).normalized

    def cache_path_for(self, source: str | Path) -> Path:
        """Return the deterministic credential-free mirror location for a source."""

        normalized = self.normalize_source(source)
        return self._cache_path_for_normalized(normalized)

    def resolve(self, source: str | Path, requested: PowerContextRef) -> ResolvedGitSource:
        """Resolve one explicit ref and retain its commit rather than the moving ref."""

        if not isinstance(requested, PowerContextRef):
            raise TypeError("requested must be a PowerContextRef")
        details = _source_details(source)
        cache_path = self._cache_path_for_normalized(details.normalized)

        local_head: str | None = None
        if requested.kind == "latest" and details.local_path is not None:
            local_head = self._clean_local_head(details)

        self._ensure_mirror(details, cache_path)
        ref = self._ref_to_resolve(details, requested, cache_path, local_head)
        if requested.kind in {"branch", "tag"}:
            self._verify_exact_ref(cache_path, ref)
        sha = self._resolve_commit(cache_path, ref)
        return ResolvedGitSource(
            source=details.normalized,
            requested=requested,
            sha=sha,
            cache_path=cache_path,
        )

    def _cache_path_for_normalized(self, normalized: str) -> Path:
        bucket = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self._cache_root / bucket

    def materialize(self, resolved: ResolvedGitSource, target: str | Path) -> Path:
        """Clone and detach at the already-resolved SHA without consulting a moving ref."""

        if not isinstance(resolved, ResolvedGitSource):
            raise TypeError("resolved must be a ResolvedGitSource")
        target_path = Path(os.path.abspath(Path(target).expanduser()))
        if os.path.lexists(target_path):
            raise GitSourceError(f"Materialization target must not exist: {target_path}")
        target_path.parent.mkdir(parents=True, exist_ok=True)

        self._git(
            ["git", "clone", "--no-checkout", str(resolved.cache_path), str(target_path)],
            cwd=target_path.parent,
            action="could not clone resolved Git source",
        )
        self._git(
            ["git", "checkout", "--detach", resolved.sha],
            cwd=target_path,
            action="could not check out resolved Git commit",
        )
        actual = self._resolve_commit(target_path, "HEAD")
        if actual != resolved.sha:
            raise GitSourceError(
                f"Materialized HEAD did not match resolved commit: expected {resolved.sha}, got {actual}"
            )
        return target_path

    def _clean_local_head(self, details: _SourceDetails) -> str:
        assert details.local_path is not None
        if not details.local_path.is_dir():
            raise GitSourceError(f"Local Git source does not exist: {details.normalized}")
        bare = self._git(
            ["git", "rev-parse", "--is-bare-repository"],
            cwd=details.local_path,
            action="could not inspect local Git source",
            secrets=details.secrets,
        )
        if bare.stdout.strip() not in {"true", "false"}:
            raise GitSourceError("Local Git source returned an invalid bare-repository status")
        if bare.stdout.strip() == "false":
            status = self._git(
                ["git", "status", "--porcelain"],
                cwd=details.local_path,
                action="could not inspect local Git source",
                secrets=details.secrets,
            )
            if status.stdout:
                raise GitSourceError("Local latest requires a clean Git working tree")
        return self._resolve_commit(details.local_path, "HEAD", secrets=details.secrets)

    def _ensure_mirror(self, details: _SourceDetails, cache_path: Path) -> None:
        self._cache_root.mkdir(parents=True, exist_ok=True)
        if not cache_path.exists():
            self._git(
                ["git", "clone", "--mirror", details.transport, str(cache_path)],
                cwd=self._cache_root,
                action="could not clone Git mirror",
                secrets=details.secrets,
            )
            self._set_origin_url(cache_path, details.normalized, secrets=details.secrets)
            return

        if not cache_path.is_dir():
            raise GitSourceError(f"Git cache path is not a directory: {cache_path}")
        bare = self._git(
            ["git", "rev-parse", "--is-bare-repository"],
            cwd=cache_path,
            action="could not validate Git mirror",
        )
        if bare.stdout.strip() != "true":
            raise GitSourceError(f"Git cache path is not a bare mirror: {cache_path}")

        self._set_origin_url(cache_path, details.transport, secrets=details.secrets)
        try:
            self._git(
                ["git", "fetch", "--prune", "origin"],
                cwd=cache_path,
                action="could not refresh Git mirror",
                secrets=details.secrets,
            )
        finally:
            self._set_origin_url(cache_path, details.normalized, secrets=details.secrets)

    def _ref_to_resolve(
        self,
        details: _SourceDetails,
        requested: PowerContextRef,
        cache_path: Path,
        local_head: str | None,
    ) -> str:
        if requested.kind == "branch":
            return f"refs/heads/{requested.value}"
        if requested.kind == "tag":
            return f"refs/tags/{requested.value}"
        if requested.kind == "commit":
            assert requested.value is not None
            return requested.value.lower()
        if local_head is not None:
            return local_head

        self._set_origin_url(cache_path, details.transport, secrets=details.secrets)
        try:
            remote_head = self._git(
                ["git", "ls-remote", "origin", "HEAD"],
                cwd=cache_path,
                action="could not resolve remote HEAD",
                secrets=details.secrets,
            )
        finally:
            self._set_origin_url(cache_path, details.normalized, secrets=details.secrets)
        matches = [
            line.split()[0].lower()
            for line in remote_head.stdout.splitlines()
            if len(line.split()) == 2 and line.split()[1] == "HEAD"
        ]
        if len(matches) != 1 or _FULL_LOWERCASE_SHA.fullmatch(matches[0]) is None:
            raise GitSourceError("Remote HEAD was missing or ambiguous")
        return matches[0]

    def _resolve_commit(self, cwd: Path, ref: str, *, secrets: tuple[str, ...] = ()) -> str:
        try:
            result = self._runner.run(
                ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                cwd=cwd,
                secrets=secrets,
            )
        except CommandError as error:
            raise GitSourceError("Git reference could not resolve to a commit") from error
        candidates = result.stdout.splitlines()
        if len(candidates) != 1:
            raise GitSourceError("Git reference could not resolve unambiguously to a commit")
        sha = candidates[0].strip().lower()
        if _FULL_LOWERCASE_SHA.fullmatch(sha) is None:
            raise GitSourceError("Git returned a non-canonical commit SHA")
        return sha

    def _verify_exact_ref(self, cache_path: Path, ref: str) -> None:
        self._git(
            ["git", "show-ref", "--verify", "--quiet", ref],
            cwd=cache_path,
            action="Git reference could not resolve exactly",
        )

    def _set_origin_url(self, cache_path: Path, url: str, *, secrets: tuple[str, ...]) -> None:
        self._git(
            ["git", "remote", "set-url", "origin", url],
            cwd=cache_path,
            action="could not configure Git mirror origin",
            secrets=secrets,
        )

    def _git(
        self,
        argv: list[str],
        *,
        cwd: Path,
        action: str,
        secrets: tuple[str, ...] = (),
    ) -> CommandResult:
        try:
            return self._runner.run(argv, cwd=cwd, secrets=secrets)
        except CommandError as error:
            raise GitSourceError(action) from error


def _source_details(source: str | Path) -> _SourceDetails:
    raw = str(source)
    if not raw or "\0" in raw:
        raise GitSourceError("Git source must be a non-empty path or URL without NUL")

    scp_match = _SCP_STYLE_URL.fullmatch(raw) if "://" not in raw else None
    if scp_match is not None:
        normalized = f"{scp_match.group('host')}:{scp_match.group('path')}"
        user = scp_match.group("user")
        secrets = tuple(secret for secret in (raw, unquote(user) if user else None) if secret)
        return _SourceDetails(normalized=normalized, transport=raw, local_path=None, secrets=secrets)

    parsed = urlsplit(raw)
    if parsed.scheme:
        if parsed.scheme not in _SUPPORTED_URL_SCHEMES:
            raise GitSourceError(f"Unsupported Git URL scheme: {parsed.scheme}")
        if not parsed.hostname:
            raise GitSourceError("Git URL must contain a host")
        try:
            port = parsed.port
        except ValueError as error:
            raise GitSourceError("Git URL contains an invalid port") from error
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        netloc = f"{host}:{port}" if port is not None else host
        normalized = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        secrets = _url_secrets(raw, parsed.username, parsed.password, parsed.query, parsed.fragment)
        return _SourceDetails(normalized=normalized, transport=raw, local_path=None, secrets=secrets)

    local_path = Path(source).expanduser().resolve()
    normalized = str(local_path)
    return _SourceDetails(normalized=normalized, transport=normalized, local_path=local_path, secrets=())


def _url_secrets(
    raw: str,
    username: str | None,
    password: str | None,
    query: str,
    fragment: str,
) -> tuple[str, ...]:
    values = [raw]
    values.extend(unquote(value) for value in (username, password) if value)
    values.extend(unquote(value) for pair in parse_qsl(query, keep_blank_values=True) for value in pair if value)
    if fragment:
        values.append(unquote(fragment))
    return tuple(dict.fromkeys(value for value in values if value))


__all__ = ["GitSource", "ResolvedGitSource"]
