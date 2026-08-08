from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_service.config import Settings
from agent_service import dsl
from agent_service.dsl import build_golden_plan, validate_test_plan
from agent_service.execution.security import assert_execution_allowed
from agent_service.schemas import (
    ApprovalDecision,
    RelatedBug,
    Requirement,
    RequirementSet,
    RiskAnalysis,
    RiskItem,
    SourceRef,
    TestAssertion as DslTestAssertion,
    TestCase as DslTestCase,
    TestPlan as DslTestPlan,
    TestStep as DslTestStep,
)


def make_plan() -> DslTestPlan:
    return DslTestPlan(
        summary="Web2 internal transfer",
        cases=[
            DslTestCase(
                case_id="TC-OTI-002",
                title="内部转账成功",
                priority="P0",
                source_refs=["人工基准:TC-OTI-002"],
                inferred=False,
                rationale="Existing manual baseline",
                preconditions=["付款账号余额充足"],
                steps=[
                    DslTestStep(action="open_internal_transfer"),
                    DslTestStep(action="fill_amount", value="10"),
                    DslTestStep(action="submit"),
                ],
                assertions=[
                    {"type": "transfer_request_succeeded"},
                    {"type": "transaction_record_created"},
                ],
            )
        ],
    )


TASK_2_MODELS = [
    SourceRef,
    Requirement,
    RequirementSet,
    RiskItem,
    RiskAnalysis,
    RelatedBug,
    DslTestStep,
    DslTestAssertion,
    DslTestCase,
    DslTestPlan,
    ApprovalDecision,
]

GOLDEN_SET_RULES = {
    "TC-OTI-001": {
        "title": "内部转账页面正常打开",
        "steps": [{"action": "open_internal_transfer"}],
        "assertions": [{"type": "page_loaded"}],
    },
    "TC-OTI-002": {
        "title": "内部转账成功",
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
        "title": "收款人不能为空",
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
        "title": "金额不能为空或为 0",
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
        "title": "余额不足时禁止转账",
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
                "expected": "可用余额不足",
            },
            {"type": "request_not_sent"},
        ],
    },
    "TC-OTI-006": {
        "title": "重复点击提交只产生一笔交易",
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


def make_golden_plan() -> DslTestPlan:
    cases = []
    for case_id, rule in GOLDEN_SET_RULES.items():
        cases.append(
            DslTestCase(
                case_id=case_id,
                title=rule["title"],
                priority="P0",
                source_refs=[f"人工基准:{case_id}"],
                inferred=False,
                rationale="Existing manual baseline",
                preconditions=[],
                steps=[
                    DslTestStep.model_validate(step)
                    for step in rule["steps"]
                ],
                assertions=[
                    DslTestAssertion.model_validate(assertion)
                    for assertion in rule["assertions"]
                ],
            )
        )
    return DslTestPlan(summary="Web2 internal transfer Golden Set", cases=cases)


def test_production_golden_plan_is_complete_and_strictly_valid() -> None:
    plan = build_golden_plan()

    assert validate_test_plan(plan, require_golden_set=True) == plan
    assert [case.case_id for case in plan.cases] == sorted(
        dsl.REQUIRED_BASELINE_IDS
    )


def approve_plan(plan: DslTestPlan) -> ApprovalDecision:
    return ApprovalDecision(
        action="approve",
        plan_hash=dsl.plan_fingerprint(plan),
    )


def test_valid_plan_is_accepted() -> None:
    plan = make_plan()
    validated = validate_test_plan(plan)

    assert validated is not plan
    assert validated.cases[0].case_id == "TC-OTI-002"


@pytest.mark.parametrize("model", TASK_2_MODELS)
def test_all_task_2_models_use_strict_model(model: type) -> None:
    assert model.__mro__[1].__name__ == "StrictModel"
    assert model.model_config["extra"] == "forbid"
    assert model.model_config["validate_assignment"] is True


@pytest.mark.parametrize("field", ["selector", "url", "python", "shell", "sql"])
def test_step_rejects_unapproved_extra_fields(field: str) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DslTestStep.model_validate({"action": "submit", field: "untrusted"})


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DslTestStep(action="run_shell", value="rm -rf /")


def test_assignment_mutation_is_rejected() -> None:
    step = DslTestStep(action="submit")

    with pytest.raises(ValidationError):
        step.action = "run_shell"


def test_revalidation_rejects_raw_dictionary_appended_to_step_list() -> None:
    plan = make_plan()
    plan.cases[0].steps.append(
        {"action": "submit", "shell": "rm -rf /"}  # type: ignore[arg-type]
    )

    with pytest.raises(ValidationError, match="extra_forbidden"):
        validate_test_plan(plan)


def test_execution_gate_rejects_raw_dictionary_appended_to_step_list(
    tmp_path: Path,
) -> None:
    plan = make_plan()
    approval = approve_plan(plan)
    plan.cases[0].steps.append(
        {"action": "submit", "shell": "rm -rf /"}  # type: ignore[arg-type]
    )

    with pytest.raises(ValidationError, match="extra_forbidden"):
        assert_execution_allowed(
            make_settings(tmp_path),
            plan,
            approval,
        )


def test_execution_gate_checks_approval_before_plan_revalidation(
    tmp_path: Path,
) -> None:
    plan = make_plan()
    plan.cases[0].steps.append(
        {"action": "submit", "shell": "rm -rf /"}  # type: ignore[arg-type]
    )

    with pytest.raises(PermissionError, match="approved"):
        assert_execution_allowed(make_settings(tmp_path), plan, None)


def test_inferred_case_requires_rationale() -> None:
    plan = make_plan()
    data = plan.cases[0].model_dump()
    data.update(case_id="AI-001", inferred=True, rationale="")
    with pytest.raises(ValidationError, match="rationale"):
        DslTestCase.model_validate(data)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        test_base_url="https://wallet-test.local",
        allowed_test_origins=["https://wallet-test.local"],
        source_paths=[],
        agent_db_path=tmp_path / "agent.sqlite3",
        artifacts_dir=tmp_path / "artifacts",
        agent_api_token="test-agent-token",
    )


def test_duplicate_case_ids_are_rejected() -> None:
    plan = make_plan()
    plan.cases.append(plan.cases[0].model_copy(deep=True))

    with pytest.raises(ValueError, match="unique"):
        validate_test_plan(plan)


def test_golden_set_is_required_when_requested() -> None:
    with pytest.raises(ValueError, match="missing Golden Set cases"):
        validate_test_plan(make_plan(), require_golden_set=True)


def test_complete_golden_set_is_accepted() -> None:
    validated = validate_test_plan(
        make_golden_plan(),
        require_golden_set=True,
    )

    assert len(validated.cases) == 6


@pytest.mark.parametrize("case_id", GOLDEN_SET_RULES)
def test_golden_set_requires_exact_title(case_id: str) -> None:
    plan = make_golden_plan()
    case = next(item for item in plan.cases if item.case_id == case_id)
    case.title = f"错误标题 {case_id}"

    with pytest.raises(ValueError, match="title"):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize("case_id", GOLDEN_SET_RULES)
def test_golden_set_requires_p0_priority(case_id: str) -> None:
    plan = make_golden_plan()
    case = next(item for item in plan.cases if item.case_id == case_id)
    case.priority = "P1"

    with pytest.raises(ValueError, match="priority"):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize("case_id", GOLDEN_SET_RULES)
def test_golden_set_requires_manual_baseline_source(case_id: str) -> None:
    plan = make_golden_plan()
    case = next(item for item in plan.cases if item.case_id == case_id)
    case.source_refs = ["PRD:internal-transfer"]

    with pytest.raises(ValueError, match="source"):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize("case_id", GOLDEN_SET_RULES)
def test_golden_set_allows_additional_prd_and_bug_sources(
    case_id: str,
) -> None:
    plan = make_golden_plan()
    case = golden_case(plan, case_id)
    case.source_refs.extend(
        ["PRD:web2-internal-transfer", "Bug:1227"],
    )

    validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize("case_id", GOLDEN_SET_RULES)
def test_golden_set_requires_each_baseline_action(case_id: str) -> None:
    plan = make_golden_plan()
    case = next(item for item in plan.cases if item.case_id == case_id)
    if len(case.steps) == 1:
        case.steps[0] = DslTestStep(action="login")
    else:
        case.steps.pop()

    with pytest.raises(ValueError, match="action"):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize("case_id", GOLDEN_SET_RULES)
def test_golden_set_requires_each_baseline_assertion(case_id: str) -> None:
    plan = make_golden_plan()
    case = next(item for item in plan.cases if item.case_id == case_id)
    case.assertions.pop()

    with pytest.raises(ValueError, match="assertion"):
        validate_test_plan(plan, require_golden_set=True)


def test_identical_cases_cannot_fake_golden_set() -> None:
    fake_cases = []
    for case_id in GOLDEN_SET_RULES:
        fake_cases.append(
            DslTestCase(
                case_id=case_id,
                title=f"Fake baseline {case_id}",
                priority="P0",
                source_refs=[f"人工基准:{case_id}"],
                inferred=False,
                rationale="Copied content",
                preconditions=[],
                steps=[DslTestStep(action="open_internal_transfer")],
                assertions=[DslTestAssertion(type="page_loaded")],
            )
        )
    plan = DslTestPlan(summary="Copied Golden Set", cases=fake_cases)

    with pytest.raises(ValueError, match="Golden Set"):
        validate_test_plan(plan, require_golden_set=True)


def golden_case(plan: DslTestPlan, case_id: str) -> DslTestCase:
    return next(case for case in plan.cases if case.case_id == case_id)


@pytest.mark.parametrize(
    "bad_step",
    [
        DslTestStep(action="fill_amount", value="-999"),
        DslTestStep(action="fill_amount", value="11"),
        DslTestStep(action="fill_amount", source="valid_transfer_amount"),
        DslTestStep(
            action="fill_amount",
            source="amount_above_available_balance",
        ),
    ],
)
@pytest.mark.parametrize("case_id", ["TC-OTI-002", "TC-OTI-006"])
def test_success_golden_case_requires_fixed_amount_ten(
    case_id: str,
    bad_step: DslTestStep,
) -> None:
    plan = make_golden_plan()
    case = golden_case(plan, case_id)
    case.steps[1] = bad_step

    with pytest.raises(ValueError, match="amount.*10"):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize("case_id", ["TC-OTI-002", "TC-OTI-006"])
def test_success_golden_case_rejects_reversed_key_steps(
    case_id: str,
) -> None:
    plan = make_golden_plan()
    case = golden_case(plan, case_id)
    case.steps.reverse()

    with pytest.raises(ValueError, match="order"):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize("case_id", ["TC-OTI-002", "TC-OTI-006"])
def test_success_golden_case_allows_preparation_actions(
    case_id: str,
) -> None:
    plan = make_golden_plan()
    case = golden_case(plan, case_id)
    case.steps = [
        DslTestStep(action="login"),
        DslTestStep(action="open_internal_transfer"),
        DslTestStep(action="select_asset", value="USDT"),
        *case.steps,
    ]

    validate_test_plan(plan, require_golden_set=True)


def test_duplicate_submit_golden_case_requires_consecutive_submits() -> None:
    plan = make_golden_plan()
    case = golden_case(plan, "TC-OTI-006")
    case.steps.insert(-1, DslTestStep(action="refresh_transaction_history"))

    with pytest.raises(ValueError, match="consecutive"):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize(
    "assertion_type",
    ["payer_balance_decreased", "recipient_balance_increased"],
)
def test_success_golden_case_requires_both_balance_assertions(
    assertion_type: str,
) -> None:
    plan = make_golden_plan()
    case = golden_case(plan, "TC-OTI-002")
    case.assertions = [
        assertion
        for assertion in case.assertions
        if assertion.type != assertion_type
    ]

    with pytest.raises(ValueError, match="balance"):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize(
    "assertion_type",
    ["payer_balance_decreased", "recipient_balance_increased"],
)
def test_success_golden_case_requires_balance_amount_ten(
    assertion_type: str,
) -> None:
    plan = make_golden_plan()
    case = golden_case(plan, "TC-OTI-002")
    assertion = next(
        item for item in case.assertions if item.type == assertion_type
    )
    assertion.amount = "999"

    with pytest.raises(ValueError, match="amount"):
        validate_test_plan(plan, require_golden_set=True)


def test_empty_recipient_golden_case_rejects_valid_recipient() -> None:
    plan = make_golden_plan()
    case = golden_case(plan, "TC-OTI-003")
    case.steps[0] = DslTestStep(
        action="fill_recipient",
        source="recipient_account",
    )

    with pytest.raises(ValueError, match="empty recipient"):
        validate_test_plan(plan, require_golden_set=True)


def test_empty_recipient_uses_last_fill_before_first_submit() -> None:
    plan = make_golden_plan()
    case = golden_case(plan, "TC-OTI-003")
    case.steps.insert(
        1,
        DslTestStep(
            action="fill_recipient",
            source="recipient_account",
        ),
    )

    with pytest.raises(ValueError, match="last.*recipient"):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize(
    "bad_step",
    [
        DslTestStep(action="fill_amount", value="10"),
        DslTestStep(action="fill_amount", value="-1"),
        DslTestStep(action="fill_amount", source="valid_transfer_amount"),
    ],
)
def test_empty_or_zero_amount_golden_case_rejects_other_amounts(
    bad_step: DslTestStep,
) -> None:
    plan = make_golden_plan()
    case = golden_case(plan, "TC-OTI-004")
    case.steps[0] = bad_step

    with pytest.raises(ValueError, match="empty or zero"):
        validate_test_plan(plan, require_golden_set=True)


def test_empty_amount_uses_last_fill_before_first_submit() -> None:
    plan = make_golden_plan()
    case = golden_case(plan, "TC-OTI-004")
    case.steps.insert(1, DslTestStep(action="fill_amount", value="10"))

    with pytest.raises(ValueError, match="last.*amount"):
        validate_test_plan(plan, require_golden_set=True)


def test_insufficient_balance_golden_case_requires_amount_above_balance() -> None:
    plan = make_golden_plan()
    case = golden_case(plan, "TC-OTI-005")
    case.steps[0] = DslTestStep(action="fill_amount", value="0")

    with pytest.raises(ValueError, match="amount_above_available_balance"):
        validate_test_plan(plan, require_golden_set=True)


def test_insufficient_balance_uses_last_fill_before_first_submit() -> None:
    plan = make_golden_plan()
    case = golden_case(plan, "TC-OTI-005")
    case.steps.insert(1, DslTestStep(action="fill_amount", value="10"))

    with pytest.raises(ValueError, match="last.*amount"):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize(
    ("case_id", "bad_expected", "message"),
    [
        ("TC-OTI-003", "格式错误", "required"),
        ("TC-OTI-004", "格式错误", "empty or zero"),
        ("TC-OTI-005", "格式错误", "insufficient balance"),
    ],
)
def test_negative_golden_cases_require_semantic_validation_messages(
    case_id: str,
    bad_expected: str,
    message: str,
) -> None:
    plan = make_golden_plan()
    case = golden_case(plan, case_id)
    assertion = next(
        item
        for item in case.assertions
        if item.type == "validation_message_equals"
    )
    assertion.expected = bad_expected

    with pytest.raises(ValueError, match=message):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize(
    "missing_action",
    [
        "fill_recipient",
        "fill_amount",
        "complete_security_verification",
    ],
)
def test_duplicate_submit_golden_case_requires_success_inputs(
    missing_action: str,
) -> None:
    plan = make_golden_plan()
    case = golden_case(plan, "TC-OTI-006")
    case.steps = [
        step for step in case.steps if step.action != missing_action
    ]

    with pytest.raises(ValueError, match="action"):
        validate_test_plan(plan, require_golden_set=True)


@pytest.mark.parametrize("empty_field", ["steps", "assertions"])
def test_empty_steps_or_assertions_are_rejected(empty_field: str) -> None:
    plan = make_plan()
    getattr(plan.cases[0], empty_field).clear()

    with pytest.raises(ValidationError, match=empty_field):
        validate_test_plan(plan)


def test_unknown_assertion_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DslTestAssertion(type="execute_python", expected="import os")


@pytest.mark.parametrize("value", [None, "", "  "])
def test_select_asset_requires_non_blank_value(value: str | None) -> None:
    with pytest.raises(ValidationError, match="select_asset"):
        DslTestStep(action="select_asset", value=value)


def test_select_asset_accepts_non_blank_value() -> None:
    assert DslTestStep(action="select_asset", value="USDT").value == "USDT"


def test_fill_recipient_requires_exactly_one_input() -> None:
    with pytest.raises(ValidationError, match="fill_recipient"):
        DslTestStep(action="fill_recipient")

    with pytest.raises(ValidationError, match="fill_recipient"):
        DslTestStep(
            action="fill_recipient",
            value="recipient@example.com",
            source="recipient_account",
        )


@pytest.mark.parametrize(
    ("value", "source"),
    [
        ("", None),
        ("recipient@example.com", None),
        (None, "recipient_account"),
    ],
)
def test_fill_recipient_accepts_one_allowed_input(
    value: str | None,
    source: str | None,
) -> None:
    step = DslTestStep(
        action="fill_recipient",
        value=value,
        source=source,
    )

    assert step.value == value
    assert step.source == source


@pytest.mark.parametrize(
    "source",
    [
        "recipient_account",
        "valid_transfer_amount",
        "amount_above_available_balance",
    ],
)
def test_step_source_accepts_registered_values(source: str) -> None:
    action = "fill_recipient" if source == "recipient_account" else "fill_amount"
    assert DslTestStep(action=action, source=source).source == source


def test_step_source_rejects_unregistered_values() -> None:
    with pytest.raises(ValidationError):
        DslTestStep(action="fill_recipient", source="arbitrary_source")


def test_fill_amount_requires_exactly_one_input() -> None:
    with pytest.raises(ValidationError, match="fill_amount"):
        DslTestStep(action="fill_amount")

    with pytest.raises(ValidationError, match="fill_amount"):
        DslTestStep(
            action="fill_amount",
            value="10",
            source="valid_transfer_amount",
        )


@pytest.mark.parametrize("value", ["", "0", "-1", "10"])
def test_fill_amount_accepts_negative_test_values(value: str) -> None:
    assert DslTestStep(action="fill_amount", value=value).value == value


@pytest.mark.parametrize(
    "source",
    ["valid_transfer_amount", "amount_above_available_balance"],
)
def test_fill_amount_accepts_registered_amount_sources(source: str) -> None:
    assert DslTestStep(action="fill_amount", source=source).source == source


@pytest.mark.parametrize(
    "source",
    ["valid_transfer_amount", "amount_above_available_balance"],
)
def test_fill_recipient_rejects_amount_sources(source: str) -> None:
    with pytest.raises(ValidationError, match="fill_recipient"):
        DslTestStep(action="fill_recipient", source=source)


@pytest.mark.parametrize(
    "action",
    [
        "login",
        "open_internal_transfer",
        "submit",
        "complete_security_verification",
        "refresh_transaction_history",
    ],
)
@pytest.mark.parametrize(
    ("value", "source"),
    [
        ("unexpected", None),
        (None, "recipient_account"),
        (None, "valid_transfer_amount"),
        (None, "amount_above_available_balance"),
    ],
)
def test_parameterless_actions_reject_value_and_source(
    action: str,
    value: str | None,
    source: str | None,
) -> None:
    with pytest.raises(ValidationError, match="does not accept"):
        DslTestStep(action=action, value=value, source=source)


@pytest.mark.parametrize("expected", [None, "", "  "])
def test_validation_message_requires_non_blank_expected(
    expected: str | None,
) -> None:
    with pytest.raises(ValidationError, match="expected"):
        DslTestAssertion(
            type="validation_message_equals",
            expected=expected,
        )


@pytest.mark.parametrize(
    "assertion_type",
    ["payer_balance_decreased", "recipient_balance_increased"],
)
@pytest.mark.parametrize("amount", [None, "", "invalid"])
def test_balance_assertions_require_decimal_amount(
    assertion_type: str,
    amount: str | None,
) -> None:
    with pytest.raises(ValidationError, match="amount"):
        DslTestAssertion(type=assertion_type, amount=amount)


@pytest.mark.parametrize(
    "assertion_type",
    ["payer_balance_decreased", "recipient_balance_increased"],
)
@pytest.mark.parametrize("amount", ["0", "-0.01", "10.5"])
def test_balance_assertions_accept_decimal_amount(
    assertion_type: str,
    amount: str,
) -> None:
    assert DslTestAssertion(type=assertion_type, amount=amount).amount == amount


@pytest.mark.parametrize(
    "assertion_type",
    [
        "page_loaded",
        "request_not_sent",
        "transfer_request_succeeded",
        "transaction_record_created",
        "single_transaction_created",
    ],
)
@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("10", None),
        (None, "unexpected"),
    ],
)
def test_parameterless_assertions_reject_amount_and_expected(
    assertion_type: str,
    amount: str | None,
    expected: str | None,
) -> None:
    with pytest.raises(ValidationError, match="does not accept"):
        DslTestAssertion(
            type=assertion_type,
            amount=amount,
            expected=expected,
        )


@pytest.mark.parametrize("summary", ["", "  "])
def test_plan_summary_rejects_blank_strings(summary: str) -> None:
    with pytest.raises(ValidationError):
        DslTestPlan(summary=summary, cases=make_plan().cases)


def test_plan_requires_cases() -> None:
    with pytest.raises(ValidationError):
        DslTestPlan(summary="Web2 internal transfer", cases=[])


@pytest.mark.parametrize("field", ["case_id", "title"])
@pytest.mark.parametrize("value", ["", "  "])
def test_case_identity_rejects_blank_strings(field: str, value: str) -> None:
    data = make_plan().cases[0].model_dump()
    data[field] = value

    with pytest.raises(ValidationError):
        DslTestCase.model_validate(data)


@pytest.mark.parametrize("field", ["source_refs", "steps", "assertions"])
def test_case_requires_non_empty_collections(field: str) -> None:
    data = make_plan().cases[0].model_dump()
    data[field] = []

    with pytest.raises(ValidationError):
        DslTestCase.model_validate(data)


@pytest.mark.parametrize("source_ref", ["", "  "])
def test_case_source_refs_reject_blank_strings(source_ref: str) -> None:
    data = make_plan().cases[0].model_dump()
    data["source_refs"] = [source_ref]

    with pytest.raises(ValidationError):
        DslTestCase.model_validate(data)


@pytest.mark.parametrize("action", ["reject", "supplement"])
@pytest.mark.parametrize("feedback", ["", "  "])
def test_reject_and_supplement_require_feedback(
    action: str,
    feedback: str,
) -> None:
    with pytest.raises(ValidationError, match="feedback"):
        ApprovalDecision(action=action, feedback=feedback)


@pytest.mark.parametrize("action", ["approve", "cancel"])
def test_approve_and_cancel_allow_empty_feedback(action: str) -> None:
    assert ApprovalDecision(action=action).feedback == ""


@pytest.mark.parametrize(
    "plan_hash",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        "0" * 63 + " ",
    ],
)
def test_approval_plan_hash_rejects_non_sha256_values(plan_hash: str) -> None:
    with pytest.raises(ValidationError, match="plan_hash"):
        ApprovalDecision(action="approve", plan_hash=plan_hash)


def test_approval_plan_hash_allows_none_during_intent_stage() -> None:
    assert ApprovalDecision(action="approve").plan_hash is None


def test_plan_fingerprint_is_stable_for_equivalent_plans() -> None:
    first = make_plan()
    second = DslTestPlan.model_validate(first.model_dump(mode="python"))

    assert dsl.plan_fingerprint(first) == dsl.plan_fingerprint(second)
    assert len(dsl.plan_fingerprint(first)) == 64


def test_plan_fingerprint_strictly_revalidates_plan() -> None:
    plan = make_plan()
    plan.cases[0].steps.append(
        {"action": "submit", "shell": "rm -rf /"}  # type: ignore[arg-type]
    )

    with pytest.raises(ValidationError, match="extra_forbidden"):
        dsl.plan_fingerprint(plan)


def test_execution_is_blocked_before_approval(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    plan = make_plan()

    with pytest.raises(PermissionError, match="approved"):
        assert_execution_allowed(settings, plan, None)

    assert_execution_allowed(
        settings,
        plan,
        approve_plan(plan),
    )


def test_execution_rejects_approval_without_plan_hash(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="plan hash"):
        assert_execution_allowed(
            make_settings(tmp_path),
            make_plan(),
            ApprovalDecision(action="approve"),
        )


def test_execution_rejects_approval_for_replaced_valid_plan(
    tmp_path: Path,
) -> None:
    approved_plan = make_plan()
    approval = approve_plan(approved_plan)
    replacement = approved_plan.model_copy(deep=True)
    replacement.cases[0].title = "审批后替换的另一份合法计划"

    with pytest.raises(PermissionError, match="plan hash"):
        assert_execution_allowed(
            make_settings(tmp_path),
            replacement,
            approval,
        )


@pytest.mark.parametrize("action", ["reject", "cancel"])
def test_rejected_or_cancelled_plan_cannot_execute(
    tmp_path: Path,
    action: str,
) -> None:
    settings = make_settings(tmp_path)
    feedback = "测试计划不符合要求" if action == "reject" else ""
    approval = ApprovalDecision(action=action, feedback=feedback)

    with pytest.raises(PermissionError, match="approved"):
        assert_execution_allowed(settings, make_plan(), approval)


@pytest.mark.parametrize("field", ["source_id", "path", "version"])
@pytest.mark.parametrize("value", ["", "  "])
def test_source_ref_rejects_blank_identity_fields(
    field: str,
    value: str,
) -> None:
    data = {
        "source_id": "prd-web2",
        "path": "/knowledge/prd.docx",
        "version": "v1",
    }
    data[field] = value

    with pytest.raises(ValidationError):
        SourceRef.model_validate(data)


@pytest.mark.parametrize("field", ["requirement_id", "statement"])
@pytest.mark.parametrize("value", ["", "  "])
def test_requirement_rejects_blank_identity_fields(
    field: str,
    value: str,
) -> None:
    data = {
        "requirement_id": "REQ-001",
        "statement": "内部转账支持同币种转账",
        "source_refs": ["prd-web2"],
        "confirmed": True,
    }
    data[field] = value

    with pytest.raises(ValidationError):
        Requirement.model_validate(data)


@pytest.mark.parametrize("source_ref", ["", "  "])
def test_requirement_rejects_blank_source_refs(source_ref: str) -> None:
    with pytest.raises(ValidationError):
        Requirement(
            requirement_id="REQ-001",
            statement="内部转账支持同币种转账",
            source_refs=[source_ref],
            confirmed=True,
        )


def test_confirmed_requirement_requires_source() -> None:
    with pytest.raises(ValidationError, match="confirmed"):
        Requirement(
            requirement_id="REQ-001",
            statement="内部转账支持同币种转账",
            source_refs=[],
            confirmed=True,
        )


def test_unconfirmed_requirement_may_have_no_source() -> None:
    requirement = Requirement(
        requirement_id="REQ-001",
        statement="转账成功后是否发送通知待确认",
        source_refs=[],
        confirmed=False,
    )
    assert requirement.source_refs == []


@pytest.mark.parametrize("field", ["risk_id", "description"])
@pytest.mark.parametrize("value", ["", "  "])
def test_risk_rejects_blank_identity_fields(
    field: str,
    value: str,
) -> None:
    data = {
        "risk_id": "RISK-001",
        "description": "重复提交可能造成重复扣款",
        "severity": "high",
        "source_refs": ["BUG-1227"],
        "inferred": False,
    }
    data[field] = value

    with pytest.raises(ValidationError):
        RiskItem.model_validate(data)


@pytest.mark.parametrize("source_ref", ["", "  "])
def test_risk_rejects_blank_source_refs(source_ref: str) -> None:
    with pytest.raises(ValidationError):
        RiskItem(
            risk_id="RISK-001",
            description="重复提交可能造成重复扣款",
            severity="high",
            source_refs=[source_ref],
            inferred=False,
        )


def test_non_inferred_risk_requires_source() -> None:
    with pytest.raises(ValidationError, match="non-inferred"):
        RiskItem(
            risk_id="RISK-001",
            description="重复提交可能造成重复扣款",
            severity="high",
            source_refs=[],
            inferred=False,
        )


def test_inferred_risk_may_have_no_source() -> None:
    risk = RiskItem(
        risk_id="RISK-001",
        description="重复提交可能造成重复扣款",
        severity="high",
        source_refs=[],
        inferred=True,
    )
    assert risk.source_refs == []


@pytest.mark.parametrize("bug_id", [0, -1])
def test_related_bug_requires_positive_id(bug_id: int) -> None:
    with pytest.raises(ValidationError):
        RelatedBug(
            bug_id=bug_id,
            title="重复转账",
            severity=1,
            status="closed",
            resolution="fixed",
        )


@pytest.mark.parametrize("field", ["title", "status"])
@pytest.mark.parametrize("value", ["", "  "])
def test_related_bug_rejects_blank_required_text(
    field: str,
    value: str,
) -> None:
    data = {
        "bug_id": 1227,
        "title": "重复转账",
        "severity": 1,
        "status": "closed",
        "resolution": "fixed",
    }
    data[field] = value

    with pytest.raises(ValidationError):
        RelatedBug.model_validate(data)


def test_non_allowlisted_base_url_is_rejected_by_settings(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="not in ALLOWED_TEST_ORIGINS"):
        Settings(
            test_base_url="https://untrusted.example",
            allowed_test_origins=["https://wallet-test.local"],
            source_paths=[],
            agent_db_path=tmp_path / "agent.sqlite3",
            artifacts_dir=tmp_path / "artifacts",
            agent_api_token="test-agent-token",
        )
