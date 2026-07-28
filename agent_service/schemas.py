from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


ActionName = Literal[
    "login",
    "open_internal_transfer",
    "select_asset",
    "fill_recipient",
    "fill_amount",
    "submit",
    "complete_security_verification",
    "refresh_transaction_history",
]

AssertionName = Literal[
    "page_loaded",
    "validation_message_equals",
    "request_not_sent",
    "transfer_request_succeeded",
    "payer_balance_decreased",
    "recipient_balance_increased",
    "transaction_record_created",
    "single_transaction_created",
]

NonBlankStr = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SourceRef(StrictModel):
    source_id: NonBlankStr
    path: NonBlankStr
    version: NonBlankStr


class Requirement(StrictModel):
    requirement_id: NonBlankStr
    statement: NonBlankStr
    source_refs: list[NonBlankStr]
    confirmed: bool

    @model_validator(mode="after")
    def confirmed_requirements_need_sources(self) -> "Requirement":
        if self.confirmed and not self.source_refs:
            raise ValueError("confirmed requirement requires a source_ref")
        return self


class RequirementSet(StrictModel):
    scope: Literal["web2_internal_transfer"]
    requirements: list[Requirement]
    missing_rules: list[str] = Field(default_factory=list)


class RiskItem(StrictModel):
    risk_id: NonBlankStr
    description: NonBlankStr
    severity: Literal["high", "medium", "low"]
    source_refs: list[NonBlankStr]
    inferred: bool

    @model_validator(mode="after")
    def non_inferred_risks_need_sources(self) -> "RiskItem":
        if not self.inferred and not self.source_refs:
            raise ValueError("non-inferred risk requires a source_ref")
        return self


class RiskAnalysis(StrictModel):
    ambiguities: list[str]
    risks: list[RiskItem]
    bug_queries: list[str]


class RelatedBug(StrictModel):
    bug_id: int = Field(gt=0)
    title: NonBlankStr
    severity: int | None
    status: NonBlankStr
    resolution: str


class TestStep(StrictModel):
    action: ActionName
    value: str | None = None
    source: Literal[
        "recipient_account",
        "valid_transfer_amount",
        "amount_above_available_balance",
    ] | None = None

    @model_validator(mode="after")
    def validate_action_parameters(self) -> "TestStep":
        has_value = self.value is not None
        has_source = self.source is not None

        if self.action == "select_asset":
            if not has_value or not self.value.strip():
                raise ValueError("select_asset requires a non-blank value")
            if has_source:
                raise ValueError("select_asset does not accept source")
        elif self.action == "fill_recipient":
            if has_value == has_source:
                raise ValueError(
                    "fill_recipient requires exactly one of value or "
                    "recipient_account source"
                )
            if has_source and self.source != "recipient_account":
                raise ValueError(
                    "fill_recipient only accepts recipient_account source"
                )
        elif self.action == "fill_amount":
            if has_value == has_source:
                raise ValueError(
                    "fill_amount requires exactly one of value or amount source"
                )
            if has_source and self.source not in {
                "valid_transfer_amount",
                "amount_above_available_balance",
            }:
                raise ValueError(
                    "fill_amount only accepts registered amount sources"
                )
        elif has_value or has_source:
            raise ValueError(
                f"{self.action} does not accept value or source"
            )
        return self


class TestAssertion(StrictModel):
    type: AssertionName
    amount: str | None = None
    expected: str | None = None

    @model_validator(mode="after")
    def validate_assertion_parameters(self) -> "TestAssertion":
        balance_assertions = {
            "payer_balance_decreased",
            "recipient_balance_increased",
        }
        if self.type == "validation_message_equals":
            if self.expected is None or not self.expected.strip():
                raise ValueError(
                    "validation_message_equals requires non-blank expected"
                )
            if self.amount is not None:
                raise ValueError(
                    "validation_message_equals does not accept amount"
                )
        elif self.type in balance_assertions:
            if self.amount is None:
                raise ValueError(f"{self.type} requires amount")
            try:
                Decimal(self.amount)
            except (InvalidOperation, ValueError):
                raise ValueError(
                    f"{self.type} amount must be a valid Decimal"
                ) from None
            if self.expected is not None:
                raise ValueError(f"{self.type} does not accept expected")
        elif self.amount is not None or self.expected is not None:
            raise ValueError(
                f"{self.type} does not accept amount or expected"
            )
        return self


class TestCase(StrictModel):
    case_id: NonBlankStr
    title: NonBlankStr
    priority: Literal["P0", "P1", "P2"]
    source_refs: list[NonBlankStr] = Field(min_length=1)
    inferred: bool
    rationale: str
    preconditions: list[str]
    steps: list[TestStep] = Field(min_length=1)
    assertions: list[TestAssertion] = Field(min_length=1)

    @model_validator(mode="after")
    def inferred_cases_need_rationale(self) -> "TestCase":
        if self.inferred and not self.rationale.strip():
            raise ValueError("inferred case requires rationale")
        return self


class TestPlan(StrictModel):
    summary: NonBlankStr
    cases: list[TestCase] = Field(min_length=1)


class ApprovalDecision(StrictModel):
    action: Literal["approve", "reject", "supplement", "cancel"]
    feedback: str = ""
    plan_hash: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{64}$"),
    ] | None = None

    @model_validator(mode="after")
    def rejected_or_supplemented_decisions_need_feedback(
        self,
    ) -> "ApprovalDecision":
        if self.action in {"reject", "supplement"} and not self.feedback.strip():
            raise ValueError(f"{self.action} decision requires feedback")
        return self
