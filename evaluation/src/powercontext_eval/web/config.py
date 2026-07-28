"""Immutable runtime configuration for the evaluation console."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _ConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    root: Path
    database_path: Path
    run_root: Path
    frontend_dist: Path
    powercontext_source: Path
    harness_root: Path
    harness_python: Path
    raw_sample_path: Path
    codex_binary: Path
    uv_binary: Path
    auth_json: Path
    proxy_url: str
    host: str = Field(min_length=1)
    port: Annotated[int, Field(ge=1, le=65535)]
    lease_seconds: Annotated[int, Field(ge=1, le=3600)]
    poll_seconds: Annotated[float, Field(gt=0, le=30)]

    @field_validator(
        "root",
        "database_path",
        "run_root",
        "frontend_dist",
        "powercontext_source",
        "harness_root",
        "harness_python",
        "raw_sample_path",
        "codex_binary",
        "uv_binary",
        "auth_json",
    )
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Runtime paths must be absolute")
        return value


@dataclass(frozen=True, slots=True)
class WebConfig:
    """Validated process configuration; secret-bearing fields are excluded from its representation."""

    root: Path
    database_path: Path
    run_root: Path
    frontend_dist: Path
    powercontext_source: Path
    harness_root: Path
    harness_python: Path
    raw_sample_path: Path
    codex_binary: Path
    uv_binary: Path
    auth_json: Path = field(repr=False)
    proxy_url: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8080
    lease_seconds: int = 60
    poll_seconds: float = 1.0

    @classmethod
    def for_root(
        cls,
        root: Path,
        *,
        database_path: Path | None = None,
        run_root: Path | None = None,
        frontend_dist: Path | None = None,
        powercontext_source: Path | None = None,
        harness_root: Path | None = None,
        harness_python: Path | None = None,
        raw_sample_path: Path | None = None,
        codex_binary: Path | None = None,
        uv_binary: Path | None = None,
        auth_json: Path | None = None,
        proxy_url: str = "http://127.0.0.1:8081",
        host: str = "127.0.0.1",
        port: int = 8080,
        lease_seconds: int = 60,
        poll_seconds: float = 1.0,
    ) -> Self:
        values = _ConfigInput.model_validate(
            {
                "root": root,
                "database_path": database_path or root / "web" / "tasks.sqlite3",
                "run_root": run_root or root,
                "frontend_dist": frontend_dist or root / "deploy" / "powercontext" / "evaluation" / "web" / "dist",
                "powercontext_source": powercontext_source or root / "powercontext",
                "harness_root": harness_root or root / "swebench-pro",
                "harness_python": harness_python or root / "swebench-pro" / ".venv" / "bin" / "python",
                "raw_sample_path": raw_sample_path or root / "data" / "swebench-pro-instance.jsonl",
                "codex_binary": codex_binary or root / "bin" / "codex",
                "uv_binary": uv_binary or root / "bin" / "uv",
                "auth_json": auth_json or root / "config" / "auth.json",
                "proxy_url": proxy_url,
                "host": host,
                "port": port,
                "lease_seconds": lease_seconds,
                "poll_seconds": poll_seconds,
            },
            strict=True,
        )
        return cls(**values.model_dump())

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Self:
        prefix = "POWERCONTEXT_EVAL_"
        root = Path(environ[f"{prefix}ROOT"])

        def path(name: str) -> Path | None:
            value = environ.get(f"{prefix}{name}")
            return None if value is None else Path(value)

        return cls.for_root(
            root,
            database_path=path("DATABASE_PATH"),
            run_root=path("RUN_ROOT"),
            frontend_dist=path("FRONTEND_DIST"),
            powercontext_source=path("POWERCONTEXT_SOURCE"),
            harness_root=path("HARNESS_ROOT"),
            harness_python=path("HARNESS_PYTHON"),
            raw_sample_path=path("RAW_SAMPLE_PATH"),
            codex_binary=path("CODEX_BINARY"),
            uv_binary=path("UV_BINARY"),
            auth_json=path("AUTH_JSON"),
            proxy_url=environ.get(f"{prefix}PROXY_URL", "http://127.0.0.1:8081"),
            host=environ.get(f"{prefix}HOST", "127.0.0.1"),
            port=int(environ.get(f"{prefix}PORT", "8080")),
            lease_seconds=int(environ.get(f"{prefix}LEASE_SECONDS", "60")),
            poll_seconds=float(environ.get(f"{prefix}POLL_SECONDS", "1")),
        )
