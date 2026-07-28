"""Pinned SWE-bench Pro dataset contract."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from powercontext_eval.errors import PowerContextEvalError

HARNESS_COMMIT = "ca10a60a5fcae51e6948ffe1485d4153d421e6c5"
DATASET_REVISION = "7ab5114912baf22bb098818e604c02fe7ad2c11f"

_FIELDS = frozenset(
    {
        "repo",
        "instance_id",
        "base_commit",
        "patch",
        "test_patch",
        "problem_statement",
        "requirements",
        "interface",
        "repo_language",
        "fail_to_pass",
        "pass_to_pass",
        "issue_specificity",
        "issue_categories",
        "before_repo_set_cmd",
        "selected_test_files_to_run",
        "dockerhub_tag",
    }
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class DatasetSchemaError(PowerContextEvalError):
    """A pinned dataset row does not match the expected public schema."""


@dataclass(frozen=True)
class SweBenchProInstance:
    """One immutable SWE-bench Pro instance."""

    fields: Mapping[str, str]
    docker_manifest_digest: str

    @classmethod
    def from_raw(
        cls,
        raw: Mapping[str, object],
        *,
        docker_manifest_digest: str,
    ) -> SweBenchProInstance:
        missing = sorted(_FIELDS - set(raw))
        unexpected = sorted(set(raw) - _FIELDS)
        if missing:
            raise DatasetSchemaError(f"Dataset row has missing fields: {', '.join(missing)}")
        if unexpected:
            raise DatasetSchemaError(f"Dataset row has unexpected fields: {', '.join(unexpected)}")
        invalid = sorted(key for key in _FIELDS if not isinstance(raw[key], str))
        if invalid:
            raise DatasetSchemaError(f"Dataset row fields must be strings: {', '.join(invalid)}")
        if _DIGEST.fullmatch(docker_manifest_digest) is None:
            raise DatasetSchemaError("Docker manifest digest must be an exact sha256 digest")
        values = MappingProxyType({key: str(raw[key]) for key in sorted(_FIELDS)})
        return cls(values, docker_manifest_digest)

    def __getattr__(self, name: str) -> str:
        try:
            return self.fields[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def codex_prompt(self) -> str:
        """Render only fields visible to the coding agent."""

        return (
            "Solve the following repository task.\n\n"
            f"Problem statement:\n{self.problem_statement}\n\n"
            f"Requirements:\n{self.requirements}\n\n"
            f"Interface:\n{self.interface}\n"
        )
