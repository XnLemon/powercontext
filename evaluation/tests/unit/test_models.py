import pytest
from pydantic import ValidationError

from powercontext_eval.models import Arm, PowerContextRef


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("latest", "latest", None),
        ("branch:main", "branch", "main"),
        ("tag:v0.1.0", "tag", "v0.1.0"),
        ("commit:0123456789abcdef0123456789abcdef01234567", "commit", "0123456789abcdef0123456789abcdef01234567"),
    ],
)
def test_powercontext_ref_parse_accepts_explicit_refs(raw: str, kind: str, value: str | None) -> None:
    ref = PowerContextRef.parse(raw)

    assert ref.kind == kind
    assert ref.value == value


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "main",
        "commit:0123456",
    ],
)
def test_powercontext_ref_parse_rejects_ambiguous_or_invalid_refs(raw: str) -> None:
    with pytest.raises(ValueError):
        PowerContextRef.parse(raw)


def test_powercontext_ref_is_frozen() -> None:
    ref = PowerContextRef.parse("latest")

    with pytest.raises(ValidationError):
        ref.value = "main"  # ty: ignore[invalid-assignment]


def test_arm_values_are_stable_strings() -> None:
    assert Arm.OFF == "off"
    assert Arm.ON == "on"
    assert str(Arm.OFF) == "off"
    assert str(Arm.ON) == "on"
