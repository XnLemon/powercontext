"""Side-effect-free evaluation path layout."""

from dataclasses import dataclass
from pathlib import Path

from powercontext_eval.models import Arm


@dataclass(frozen=True)
class EvaluationPaths:
    """Compute ephemeral and retained paths for one evaluation run."""

    root: Path
    run_id: str

    def __post_init__(self) -> None:
        if not self.run_id or ".." in self.run_id or "/" in self.run_id or "\\" in self.run_id:
            raise ValueError(f"Unsafe run ID: {self.run_id!r}")

    @property
    def run_artifacts(self) -> Path:
        """Return the retained artifact directory for the run."""

        return self.root / "runs" / self.run_id

    def arm_work(self, arm: Arm) -> Path:
        """Return an arm's ephemeral work directory."""

        return self.root / "work" / self.run_id / arm.value

    def arm_artifacts(self, arm: Arm) -> Path:
        """Return an arm's retained artifact directory."""

        return self.run_artifacts / "arms" / arm.value
