"""Immutable runtime configuration for the evaluation console."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from powercontext_eval.codex import DEFAULT_CODEX_MODEL, is_safe_codex_model

_SAFE_DOCKER_NETWORK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MAX_TASK_PARALLELISM = 10
MAX_TOKENSFLOW_FINALIZER_TIMEOUT_SECONDS = 600


class _EnvironmentNumbers(BaseModel):
    """Coerce textual environment values before strict runtime construction."""

    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    lease_seconds: Annotated[int, Field(ge=1, le=3600)] = 60
    poll_seconds: Annotated[float, Field(gt=0, le=30)] = 1.0
    usage_pause_percent: Annotated[int, Field(ge=1, le=100)] = 80
    usage_probe_seconds: Annotated[int, Field(ge=10, le=3600)] = 60
    usage_probe_timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 15
    usage_snapshot_max_age_seconds: Annotated[int, Field(ge=10, le=7200)] = 120
    task_parallelism: Annotated[int, Field(ge=1, le=MAX_TASK_PARALLELISM)] = 1
    tokensflow_finalizer_timeout_seconds: Annotated[int, Field(ge=60, le=MAX_TOKENSFLOW_FINALIZER_TIMEOUT_SECONDS)] = (
        600
    )
    tokensflow_finalizer_poll_seconds: Annotated[float, Field(gt=0, le=60)] = 5.0

    @field_validator("task_parallelism", mode="before")
    @classmethod
    def require_integer_task_parallelism(cls, value: object) -> object:
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            raise ValueError("Task parallelism must be an integer")
        return value


class WebConfig(BaseModel):
    """Validated process configuration with secret-bearing fields excluded from serialization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    root: Path
    database_path: Path
    run_root: Path
    frontend_dist: Path
    powercontext_source: Path
    harness_root: Path
    harness_python: Path
    dataset_path: Path
    codex_binary: Path
    tokensflow_binary: Path
    tokensflow_user_home: Path = Field(exclude=True, repr=False)
    tokensflow_egress_network: str = Field(repr=False)
    uv_binary: Path
    registry_binary: Path
    auth_json: Path = Field(exclude=True, repr=False)
    proxy_url: str = Field(exclude=True, repr=False)
    host: str = Field(default="127.0.0.1", min_length=1)
    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    lease_seconds: Annotated[int, Field(ge=1, le=3600)] = 60
    poll_seconds: Annotated[float, Field(gt=0, le=30)] = 1.0
    usage_pause_percent: Annotated[int, Field(ge=1, le=100)] = 80
    usage_probe_seconds: Annotated[int, Field(ge=10, le=3600)] = 60
    usage_probe_timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 15
    usage_snapshot_max_age_seconds: Annotated[int, Field(ge=10, le=7200)] = 120
    task_parallelism: Annotated[int, Field(ge=1, le=MAX_TASK_PARALLELISM)] = 1
    tokensflow_finalizer_timeout_seconds: Annotated[int, Field(ge=60, le=MAX_TOKENSFLOW_FINALIZER_TIMEOUT_SECONDS)] = (
        600
    )
    tokensflow_finalizer_poll_seconds: Annotated[float, Field(gt=0, le=60)] = 5.0
    codex_models: tuple[str, ...] = (DEFAULT_CODEX_MODEL,)

    @field_validator(
        "root",
        "database_path",
        "run_root",
        "frontend_dist",
        "powercontext_source",
        "harness_root",
        "harness_python",
        "dataset_path",
        "codex_binary",
        "tokensflow_binary",
        "tokensflow_user_home",
        "uv_binary",
        "registry_binary",
        "auth_json",
    )
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Runtime paths must be absolute")
        return value

    @field_validator("tokensflow_egress_network")
    @classmethod
    def require_safe_tokensflow_egress_network(cls, value: str) -> str:
        if _SAFE_DOCKER_NETWORK.fullmatch(value) is None:
            raise ValueError("TokensFlow egress network is unsafe")
        return value

    @field_validator("codex_models")
    @classmethod
    def require_safe_codex_models_with_default(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        deduplicated = tuple(dict.fromkeys(value))
        if not deduplicated or any(not is_safe_codex_model(model) for model in deduplicated):
            raise ValueError("Codex model allowlist is unsafe")
        if DEFAULT_CODEX_MODEL not in deduplicated:
            raise ValueError("Codex model allowlist must include the default model")
        return deduplicated

    @model_validator(mode="after")
    def require_usage_snapshot_to_cover_probe_interval(self) -> Self:
        if self.usage_snapshot_max_age_seconds < self.usage_probe_seconds:
            raise ValueError("Usage snapshot max age must cover at least one probe interval")
        return self

    @classmethod
    def for_root(
        cls,
        root: Path,
        *,
        tokensflow_egress_network: str,
        database_path: Path | None = None,
        run_root: Path | None = None,
        frontend_dist: Path | None = None,
        powercontext_source: Path | None = None,
        harness_root: Path | None = None,
        harness_python: Path | None = None,
        dataset_path: Path | None = None,
        raw_sample_path: Path | None = None,
        codex_binary: Path | None = None,
        tokensflow_binary: Path | None = None,
        tokensflow_user_home: Path | None = None,
        uv_binary: Path | None = None,
        registry_binary: Path | None = None,
        auth_json: Path | None = None,
        proxy_url: str = "http://127.0.0.1:7890",
        host: str = "127.0.0.1",
        port: int = 8080,
        lease_seconds: int = 60,
        poll_seconds: float = 1.0,
        usage_pause_percent: int = 80,
        usage_probe_seconds: int = 60,
        usage_probe_timeout_seconds: int = 15,
        usage_snapshot_max_age_seconds: int = 120,
        task_parallelism: int = 1,
        tokensflow_finalizer_timeout_seconds: int = 600,
        tokensflow_finalizer_poll_seconds: float = 5.0,
        codex_models: tuple[str, ...] = (DEFAULT_CODEX_MODEL,),
    ) -> Self:
        return cls(
            root=root,
            database_path=database_path or root / "web" / "tasks.sqlite3",
            run_root=run_root or root,
            frontend_dist=frontend_dist or root / "deploy" / "powercontext" / "evaluation" / "web" / "dist",
            powercontext_source=powercontext_source or root / "source" / "powercontext.git",
            harness_root=harness_root or root / "cache" / "swebench-pro.git",
            harness_python=harness_python or root / "venvs" / "swebench-pro-ca10a60" / "bin" / "python",
            dataset_path=dataset_path
            or raw_sample_path
            or root / "cache" / "swebench-pro.git" / "helper_code" / "sweap_eval_full_v2.jsonl",
            codex_binary=codex_binary or root / "bin" / "codex",
            tokensflow_binary=tokensflow_binary or root / "bin" / "tokensflow",
            tokensflow_user_home=tokensflow_user_home or root / "tokensflow-home",
            tokensflow_egress_network=tokensflow_egress_network,
            uv_binary=uv_binary or root / "bin" / "uv",
            registry_binary=registry_binary or root / "bin" / "regctl",
            auth_json=auth_json or root / "codex-home" / "auth.json",
            proxy_url=proxy_url,
            host=host,
            port=port,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
            usage_pause_percent=usage_pause_percent,
            usage_probe_seconds=usage_probe_seconds,
            usage_probe_timeout_seconds=usage_probe_timeout_seconds,
            usage_snapshot_max_age_seconds=usage_snapshot_max_age_seconds,
            task_parallelism=task_parallelism,
            tokensflow_finalizer_timeout_seconds=tokensflow_finalizer_timeout_seconds,
            tokensflow_finalizer_poll_seconds=tokensflow_finalizer_poll_seconds,
            codex_models=codex_models,
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> Self:
        prefix = "POWERCONTEXT_EVAL_"
        root = Path(environ[f"{prefix}ROOT"])

        def path(name: str) -> Path | None:
            value = environ.get(f"{prefix}{name}")
            return None if value is None else Path(value)

        numbers = _EnvironmentNumbers.model_validate(
            {
                "port": environ.get(f"{prefix}PORT", "8080"),
                "lease_seconds": environ.get(f"{prefix}LEASE_SECONDS", "60"),
                "poll_seconds": environ.get(f"{prefix}POLL_SECONDS", "1"),
                "usage_pause_percent": environ.get(f"{prefix}USAGE_PAUSE_PERCENT", "80"),
                "usage_probe_seconds": environ.get(f"{prefix}USAGE_PROBE_SECONDS", "60"),
                "usage_probe_timeout_seconds": environ.get(f"{prefix}USAGE_PROBE_TIMEOUT_SECONDS", "15"),
                "usage_snapshot_max_age_seconds": environ.get(f"{prefix}USAGE_SNAPSHOT_MAX_AGE_SECONDS", "120"),
                "task_parallelism": environ.get(f"{prefix}TASK_PARALLELISM", "1"),
                "tokensflow_finalizer_timeout_seconds": environ.get(
                    f"{prefix}TOKENSFLOW_FINALIZER_TIMEOUT_SECONDS", "600"
                ),
                "tokensflow_finalizer_poll_seconds": environ.get(f"{prefix}TOKENSFLOW_FINALIZER_POLL_SECONDS", "5"),
            }
        )

        return cls.for_root(
            root,
            database_path=path("DATABASE_PATH"),
            run_root=path("RUN_ROOT"),
            frontend_dist=path("FRONTEND_DIST"),
            powercontext_source=path("POWERCONTEXT_SOURCE"),
            harness_root=path("HARNESS_ROOT"),
            harness_python=path("HARNESS_PYTHON"),
            dataset_path=path("DATASET_PATH") or path("RAW_SAMPLE_PATH"),
            codex_binary=path("CODEX_BINARY"),
            tokensflow_binary=path("TOKENSFLOW_BINARY"),
            tokensflow_user_home=path("TOKENSFLOW_USER_HOME"),
            tokensflow_egress_network=environ[f"{prefix}TOKENSFLOW_EGRESS_NETWORK"],
            uv_binary=path("UV_BINARY"),
            registry_binary=path("REGISTRY_BINARY"),
            auth_json=path("AUTH_JSON"),
            proxy_url=environ.get(f"{prefix}PROXY_URL", "http://127.0.0.1:7890"),
            host=environ.get(f"{prefix}HOST", "127.0.0.1"),
            port=numbers.port,
            lease_seconds=numbers.lease_seconds,
            poll_seconds=numbers.poll_seconds,
            usage_pause_percent=numbers.usage_pause_percent,
            usage_probe_seconds=numbers.usage_probe_seconds,
            usage_probe_timeout_seconds=numbers.usage_probe_timeout_seconds,
            usage_snapshot_max_age_seconds=numbers.usage_snapshot_max_age_seconds,
            task_parallelism=numbers.task_parallelism,
            tokensflow_finalizer_timeout_seconds=numbers.tokensflow_finalizer_timeout_seconds,
            tokensflow_finalizer_poll_seconds=numbers.tokensflow_finalizer_poll_seconds,
            codex_models=tuple(environ.get(f"{prefix}CODEX_MODELS", DEFAULT_CODEX_MODEL).split(",")),
        )

    @property
    def raw_sample_path(self) -> Path:
        """Compatibility alias while the runner migrates to catalog instances."""

        return self.dataset_path

    def accepts_codex_model(self, model: str) -> bool:
        """Apply the current admission policy to newly submitted work only."""

        return is_safe_codex_model(model) and model in self.codex_models
