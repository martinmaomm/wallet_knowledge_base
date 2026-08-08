from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable

from agent_service.schemas import TestCase, TestPlan


REQUIRED_BASELINE_IDS = {
    "TC-OTI-001",
    "TC-OTI-002",
    "TC-OTI-003",
    "TC-OTI-004",
    "TC-OTI-005",
    "TC-OTI-006",
}

GOLDEN_TITLES = {
    "TC-OTI-001": "内部转账页面正常打开",
    "TC-OTI-002": "内部转账成功",
    "TC-OTI-003": "收款人不能为空",
    "TC-OTI-004": "金额不能为空或为 0",
    "TC-OTI-005": "余额不足时禁止转账",
    "TC-OTI-006": "重复点击提交只产生一笔交易",
}

GOLDEN_CASE_DEFINITIONS = {
    "TC-OTI-001": {
        "steps": [{"action": "open_internal_transfer"}],
        "assertions": [{"type": "page_loaded"}],
    },
    "TC-OTI-002": {
        "steps": [
            {"action": "fill_recipient", "source": "recipient_account"},
            {"action": "fill_amount", "value": "10"},
            {"action": "complete_security_verification"},
            {"action": "submit"},
        ],
        "assertions": [
            {"type": "transfer_request_succeeded"},
            {"type": "payer_balance_decreased", "amount": "10"},
            {"type": "recipient_balance_increased", "amount": "10"},
            {"type": "transaction_record_created"},
        ],
    },
    "TC-OTI-003": {
        "steps": [
            {"action": "fill_recipient", "value": ""},
            {"action": "submit"},
        ],
        "assertions": [
            {
                "type": "validation_message_equals",
                "expected": "收款人不能为空",
            },
            {"type": "request_not_sent"},
        ],
    },
    "TC-OTI-004": {
        "steps": [
            {"action": "fill_amount", "value": "0"},
            {"action": "submit"},
        ],
        "assertions": [
            {
                "type": "validation_message_equals",
                "expected": "金额不能为空或必须大于 0",
            },
            {"type": "request_not_sent"},
        ],
    },
    "TC-OTI-005": {
        "steps": [
            {
                "action": "fill_amount",
                "source": "amount_above_available_balance",
            },
            {"action": "submit"},
        ],
        "assertions": [
            {
                "type": "validation_message_equals",
                "expected": "余额不足",
            },
            {"type": "request_not_sent"},
        ],
    },
    "TC-OTI-006": {
        "steps": [
            {"action": "fill_recipient", "source": "recipient_account"},
            {"action": "fill_amount", "value": "10"},
            {"action": "complete_security_verification"},
            {"action": "submit"},
            {"action": "submit"},
        ],
        "assertions": [{"type": "single_transaction_created"}],
    },
}


def _require_actions(case: TestCase, **required: int) -> None:
    actual = Counter(step.action for step in case.steps)
    missing = {
        action: count - actual[action]
        for action, count in required.items()
        if actual[action] < count
    }
    if missing:
        raise ValueError(
            f"Golden Set {case.case_id} missing action baseline: {missing}"
        )


def _require_assertion(case: TestCase, assertion_type: str) -> None:
    if not any(
        assertion.type == assertion_type for assertion in case.assertions
    ):
        raise ValueError(
            f"Golden Set {case.case_id} missing assertion "
            f"{assertion_type}"
        )


def _require_action_order(
    case: TestCase,
    expected_actions: list[str],
) -> None:
    relevant_actions = set(expected_actions)
    actual_actions = [
        step.action
        for step in case.steps
        if step.action in relevant_actions
    ]
    if actual_actions != expected_actions:
        raise ValueError(
            f"Golden Set {case.case_id} action order must be "
            f"{expected_actions}"
        )


def _last_step_before_first_submit(
    case: TestCase,
    action: str,
):
    first_submit_index = next(
        index
        for index, step in enumerate(case.steps)
        if step.action == "submit"
    )
    matching_steps = [
        step
        for step in case.steps[:first_submit_index]
        if step.action == action
    ]
    if not matching_steps:
        raise ValueError(
            f"Golden Set {case.case_id} missing action {action} "
            "before first submit"
        )
    return matching_steps[-1]


def _validation_messages(case: TestCase) -> list[str]:
    return [
        assertion.expected or ""
        for assertion in case.assertions
        if assertion.type == "validation_message_equals"
    ]


def _validate_001(case: TestCase) -> None:
    _require_actions(case, open_internal_transfer=1)
    _require_assertion(case, "page_loaded")


def _validate_002(case: TestCase) -> None:
    _require_actions(
        case,
        fill_recipient=1,
        fill_amount=1,
        complete_security_verification=1,
        submit=1,
    )
    expected_actions = [
        "fill_recipient",
        "fill_amount",
        "complete_security_verification",
        "submit",
    ]
    _require_action_order(case, expected_actions)
    recipient_step = next(
        step for step in case.steps if step.action == "fill_recipient"
    )
    if (
        recipient_step.source != "recipient_account"
        or recipient_step.value is not None
    ):
        raise ValueError(
            "Golden Set TC-OTI-002 requires recipient_account source"
        )
    amount_step = next(
        step for step in case.steps if step.action == "fill_amount"
    )
    if amount_step.value != "10" or amount_step.source is not None:
        raise ValueError(
            "Golden Set TC-OTI-002 amount must use fixed value 10"
        )

    _require_assertion(case, "transfer_request_succeeded")
    for assertion_type in {
        "payer_balance_decreased",
        "recipient_balance_increased",
    }:
        matching = [
            assertion
            for assertion in case.assertions
            if assertion.type == assertion_type
        ]
        if not matching:
            raise ValueError(
                f"Golden Set TC-OTI-002 missing balance assertion "
                f"{assertion_type}"
            )
        if not any(assertion.amount == "10" for assertion in matching):
            raise ValueError(
                f"Golden Set TC-OTI-002 balance assertion amount "
                f"must be 10 for {assertion_type}"
            )
    _require_assertion(case, "transaction_record_created")


def _validate_003(case: TestCase) -> None:
    _require_actions(case, fill_recipient=1, submit=1)
    recipient_step = _last_step_before_first_submit(
        case,
        "fill_recipient",
    )
    if not (
        recipient_step.value == ""
        and recipient_step.source is None
    ):
        raise ValueError(
            "Golden Set TC-OTI-003 last recipient before first submit "
            "must be an empty recipient value"
        )

    messages = _validation_messages(case)
    required_terms = ("必填", "不能为空", "请输入收款人", "required")
    if not any(
        any(term in message.lower() for term in required_terms)
        for message in messages
    ):
        raise ValueError(
            "Golden Set TC-OTI-003 requires a required-field "
            "validation assertion"
        )
    _require_assertion(case, "request_not_sent")


def _validate_004(case: TestCase) -> None:
    _require_actions(case, fill_amount=1, submit=1)
    amount_step = _last_step_before_first_submit(
        case,
        "fill_amount",
    )
    if not (
        amount_step.value in {"", "0"}
        and amount_step.source is None
    ):
        raise ValueError(
            "Golden Set TC-OTI-004 last amount before first submit "
            "must be empty or zero"
        )

    messages = _validation_messages(case)
    required_terms = (
        "金额不能为空",
        "请输入金额",
        "必须大于 0",
        "必须大于0",
        "amount is required",
        "greater than 0",
    )
    if not any(
        any(term in message.lower() for term in required_terms)
        for message in messages
    ):
        raise ValueError(
            "Golden Set TC-OTI-004 requires an empty or zero amount "
            "validation assertion"
        )
    _require_assertion(case, "request_not_sent")


def _validate_005(case: TestCase) -> None:
    _require_actions(case, fill_amount=1, submit=1)
    amount_step = _last_step_before_first_submit(
        case,
        "fill_amount",
    )
    if not (
        amount_step.source == "amount_above_available_balance"
        and amount_step.value is None
    ):
        raise ValueError(
            "Golden Set TC-OTI-005 last amount before first submit "
            "must use "
            "amount_above_available_balance source"
        )

    messages = _validation_messages(case)
    required_terms = ("余额不足", "超过可用余额", "insufficient balance")
    if not any(
        any(term in message.lower() for term in required_terms)
        for message in messages
    ):
        raise ValueError(
            "Golden Set TC-OTI-005 requires an insufficient balance "
            "validation assertion"
        )
    _require_assertion(case, "request_not_sent")


def _validate_006(case: TestCase) -> None:
    _require_actions(
        case,
        fill_recipient=1,
        fill_amount=1,
        complete_security_verification=1,
        submit=2,
    )
    expected_actions = [
        "fill_recipient",
        "fill_amount",
        "complete_security_verification",
        "submit",
        "submit",
    ]
    _require_action_order(case, expected_actions)
    recipient_step = next(
        step for step in case.steps if step.action == "fill_recipient"
    )
    if (
        recipient_step.source != "recipient_account"
        or recipient_step.value is not None
    ):
        raise ValueError(
            "Golden Set TC-OTI-006 requires recipient_account source"
        )
    amount_step = next(
        step for step in case.steps if step.action == "fill_amount"
    )
    if amount_step.value != "10" or amount_step.source is not None:
        raise ValueError(
            "Golden Set TC-OTI-006 amount must use fixed value 10"
        )
    submit_indexes = [
        index
        for index, step in enumerate(case.steps)
        if step.action == "submit"
    ]
    if submit_indexes[1] != submit_indexes[0] + 1:
        raise ValueError(
            "Golden Set TC-OTI-006 submit actions must be consecutive"
        )
    _require_assertion(case, "single_transaction_created")


GOLDEN_VALIDATORS: dict[str, Callable[[TestCase], None]] = {
    "TC-OTI-001": _validate_001,
    "TC-OTI-002": _validate_002,
    "TC-OTI-003": _validate_003,
    "TC-OTI-004": _validate_004,
    "TC-OTI-005": _validate_005,
    "TC-OTI-006": _validate_006,
}


def _validate_golden_case(case_id: str, plan: TestPlan) -> None:
    case = next(case for case in plan.cases if case.case_id == case_id)
    if case.title != GOLDEN_TITLES[case_id]:
        raise ValueError(
            f"Golden Set {case_id} title must be "
            f"{GOLDEN_TITLES[case_id]!r}"
        )
    if case.priority != "P0":
        raise ValueError(f"Golden Set {case_id} priority must be P0")

    expected_source = f"人工基准:{case_id}"
    if expected_source not in case.source_refs:
        raise ValueError(
            f"Golden Set {case_id} source must be {expected_source}"
        )

    GOLDEN_VALIDATORS[case_id](case)


def validate_test_plan(
    plan: TestPlan,
    *,
    require_golden_set: bool = False,
) -> TestPlan:
    strict_plan = TestPlan.model_validate(
        plan.model_dump(mode="python", warnings="none")
    )
    ids = [case.case_id for case in strict_plan.cases]
    if len(ids) != len(set(ids)):
        raise ValueError("test case ids must be unique")
    if require_golden_set:
        missing = sorted(REQUIRED_BASELINE_IDS.difference(ids))
        if missing:
            raise ValueError(f"missing Golden Set cases: {', '.join(missing)}")
        for case_id in sorted(REQUIRED_BASELINE_IDS):
            _validate_golden_case(case_id, strict_plan)
    return strict_plan


def build_golden_plan() -> TestPlan:
    cases = [
        TestCase.model_validate(
            {
                "case_id": case_id,
                "title": GOLDEN_TITLES[case_id],
                "priority": "P0",
                "source_refs": [f"人工基准:{case_id}"],
                "inferred": False,
                "rationale": "Version-controlled manual baseline",
                "preconditions": [],
                **GOLDEN_CASE_DEFINITIONS[case_id],
            }
        )
        for case_id in sorted(REQUIRED_BASELINE_IDS)
    ]
    plan = TestPlan(
        summary="Web2 internal transfer deterministic Golden Set",
        cases=cases,
    )
    return validate_test_plan(plan, require_golden_set=True)


def plan_fingerprint(plan: TestPlan) -> str:
    strict_plan = validate_test_plan(plan)
    canonical_json = json.dumps(
        strict_plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
