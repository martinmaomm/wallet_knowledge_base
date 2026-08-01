from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_service.execution.assertions import (
    AssertionResult,
    assert_balance_change,
    assert_single_transaction,
)
from agent_service.schemas import FailureAnalysis


def test_assertion_result_is_strict_frozen_and_forbids_extra_fields() -> None:
    result = AssertionResult(
        name="balance_change",
        passed=True,
        expected="-10",
        actual="-10",
    )

    with pytest.raises(ValidationError, match="frozen"):
        result.passed = False
    with pytest.raises(ValidationError, match="extra_forbidden"):
        AssertionResult.model_validate(
            {
                **result.model_dump(),
                "unexpected": "value",
            }
        )
    with pytest.raises(ValidationError):
        AssertionResult(
            name="balance_change",
            passed=1,
            expected="-10",
            actual="-10",
        )


def test_balance_assertion_uses_exact_finite_decimal_arithmetic() -> None:
    passed = assert_balance_change(
        before=Decimal("100.00000000"),
        after=Decimal("90.00000000"),
        expected_delta=Decimal("-10.00000000"),
    )
    failed = assert_balance_change(
        before=Decimal("100.00000000"),
        after=Decimal("90.00000001"),
        expected_delta=Decimal("-10.00000000"),
    )

    assert passed.passed is True
    assert passed.actual == "-10.00000000"
    assert failed.passed is False


@pytest.mark.parametrize(
    ("before", "after", "expected_delta"),
    [
        (Decimal("NaN"), Decimal("1"), Decimal("1")),
        (Decimal("1"), Decimal("Infinity"), Decimal("1")),
        (Decimal("1"), Decimal("2"), Decimal("-Infinity")),
    ],
)
def test_balance_assertion_rejects_non_finite_decimals(
    before: Decimal,
    after: Decimal,
    expected_delta: Decimal,
) -> None:
    with pytest.raises(ValueError, match="finite Decimal"):
        assert_balance_change(
            before=before,
            after=after,
            expected_delta=expected_delta,
        )


def test_single_transaction_uses_set_difference() -> None:
    passed = assert_single_transaction(
        before_ids={"tx-old"},
        after_ids={"tx-old", "tx-new"},
    )
    duplicate = assert_single_transaction(
        before_ids={"tx-old"},
        after_ids={"tx-old", "tx-new-1", "tx-new-2"},
    )

    assert passed.passed is True
    assert passed.actual == "1"
    assert duplicate.passed is False
    assert duplicate.actual == "2"


@pytest.mark.parametrize(
    ("before_ids", "after_ids"),
    [
        ({"tx-old"}, {""}),
        ({"tx-old"}, {"../tx-new"}),
        ({"tx-old"}, {"tx new"}),
        ({"tx-old"}, {123}),
    ],
)
def test_single_transaction_rejects_invalid_transaction_ids(
    before_ids: set[str],
    after_ids: set[str],
) -> None:
    with pytest.raises(ValueError, match="transaction id"):
        assert_single_transaction(
            before_ids=before_ids,
            after_ids=after_ids,
        )


def test_failure_analysis_is_strict_and_requires_non_blank_text() -> None:
    valid = {
        "category": "product",
        "summary": "余额断言失败",
        "evidence_refs": ["ASSERT-balance"],
        "related_bug_ids": [1227],
        "recommended_action": "检查余额更新逻辑",
    }

    assert FailureAnalysis.model_validate(valid).related_bug_ids == [1227]
    for field in ("summary", "recommended_action"):
        with pytest.raises(ValidationError):
            FailureAnalysis.model_validate({**valid, field: "  "})
    with pytest.raises(ValidationError):
        FailureAnalysis.model_validate({**valid, "evidence_refs": []})
    with pytest.raises(ValidationError):
        FailureAnalysis.model_validate({**valid, "related_bug_ids": [0]})
    with pytest.raises(ValidationError):
        FailureAnalysis.model_validate(
            {**valid, "related_bug_ids": ["1227"]}
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        FailureAnalysis.model_validate({**valid, "passed": True})
