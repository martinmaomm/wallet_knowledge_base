from __future__ import annotations

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_service.execution.runner import ExecutionResult


_TRANSACTION_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
)


class AssertionResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
    )

    name: str = Field(min_length=1)
    passed: bool
    expected: str
    actual: str


class ExecutionBackendResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
    )

    execution_results: tuple[ExecutionResult, ...] = Field(
        default_factory=tuple
    )
    assertion_results: tuple[AssertionResult, ...] = Field(min_length=1)

    @field_validator(
        "execution_results",
        "assertion_results",
        mode="before",
    )
    @classmethod
    def normalize_result_sequences(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


def _require_finite_decimal(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("balance values must be finite Decimal instances")
    return value


def assert_balance_change(
    *,
    before: Decimal,
    after: Decimal,
    expected_delta: Decimal,
) -> AssertionResult:
    validated_before = _require_finite_decimal(before)
    validated_after = _require_finite_decimal(after)
    validated_expected = _require_finite_decimal(expected_delta)
    actual_delta = validated_after - validated_before
    return AssertionResult(
        name="balance_change",
        passed=actual_delta == validated_expected,
        expected=str(validated_expected),
        actual=str(actual_delta),
    )


def _validate_transaction_ids(values: set[str]) -> set[str]:
    if not isinstance(values, set) or any(
        not isinstance(value, str)
        or _TRANSACTION_ID_PATTERN.fullmatch(value) is None
        for value in values
    ):
        raise ValueError("transaction id contains unsafe characters")
    return values


def assert_single_transaction(
    *,
    before_ids: set[str],
    after_ids: set[str],
) -> AssertionResult:
    validated_before = _validate_transaction_ids(before_ids)
    validated_after = _validate_transaction_ids(after_ids)
    created = validated_after.difference(validated_before)
    return AssertionResult(
        name="single_transaction_created",
        passed=len(created) == 1,
        expected="1",
        actual=str(len(created)),
    )
