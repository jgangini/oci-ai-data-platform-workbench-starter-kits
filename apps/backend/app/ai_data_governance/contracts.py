"""Closed model outputs. Validation of a DTO does not grant authorization."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Identifier = Annotated[str, Field(min_length=1, max_length=256)]
ShortText = Annotated[str, Field(min_length=1, max_length=2000)]
References = Annotated[list[Identifier], Field(max_length=128)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1] = 1


class GovIntent(Contract):
    intent: Literal["inventory", "meaning", "lineage", "usage", "quality", "impact",
                    "access", "case_followup", "treatment", "decision"]
    asset_mentions: list[ShortText] = Field(default_factory=list, max_length=32)
    requested_direction: Literal["upstream", "downstream", "both"] | None = None
    requested_change: ShortText | None = None
    decision_reference: Identifier | None = None
    decision_action: Literal["approve", "reject", "revoke", "unclear"] | None = None
    missing_fields: list[ShortText] = Field(default_factory=list, max_length=16)
    clarification: ShortText | None = None
    case_reference: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


class PlanStep(Contract):
    operation: Literal["search_assets", "read_asset_snapshot", "read_lineage", "find_code_usage",
                       "read_code_excerpt", "read_quality_results", "read_access_evidence", "read_case"]
    asset_ids: References
    version_ids: References
    purpose: ShortText
    limit: int = Field(ge=1, le=1000)


class GovPlan(Contract):
    goal: ShortText
    steps: list[PlanStep] = Field(max_length=16)
    stop_condition: ShortText
    missing_context: list[ShortText] = Field(default_factory=list, max_length=16)


class SupportedClaim(Contract):
    text: ShortText
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=128)
    kind: Literal["observed", "inferred", "recommendation"]


class EvidenceReview(Contract):
    evidence_ids: References
    supported_claims: list[SupportedClaim] = Field(max_length=64)
    conflicts: list[ShortText] = Field(max_length=32)
    gaps: list[ShortText] = Field(max_length=32)
    coverage: Literal["sufficient", "partial", "insufficient", "conflicting"]
    next_action: Literal["answer", "retrieve", "clarify", "not_evaluable"]


class GovDiagnosis(Contract):
    findings: list[SupportedClaim] = Field(max_length=64)
    limitations: list[ShortText] = Field(max_length=32)
    potential_impacts: list[SupportedClaim] = Field(max_length=64)
    treatment_needed: bool


class TreatmentDraft(Contract):
    problem: ShortText
    asset_ids: References
    expected_versions: References
    suggested_action: ShortText
    responsible_ref: Identifier | None
    verification_plan: ShortText
    remaining_gaps: list[ShortText] = Field(max_length=32)


class CaseAuthorityDeclaration(Contract):
    """Typed registration policy; this DTO alone supplies no authority or IAM grant."""
    schema_version: int = Field(..., strict=True, ge=1, le=1)
    purpose: Literal["CASE_REGISTRATION"]
    actor_ref: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    actions: list[Literal["OPEN", "PROPOSE", "APPROVE", "REJECT", "REVOKE"]] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_actions(self) -> "CaseAuthorityDeclaration":
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("Duplicate case registration actions")
        return self


class GovResponse(Contract):
    answer_markdown: str = Field(min_length=1, max_length=16000)
    cited_evidence_ids: References
    limitations: list[ShortText] = Field(max_length=32)
    case_ref: Identifier | None
    result_state: Literal["answered", "partial", "clarification", "denied", "not_evaluable",
                          "waiting_decision", "rejected", "assigned", "verification_failed", "verified"]


class GovConsultationResponse(GovResponse):
    case_ref: None
    result_state: Literal["answered", "partial", "clarification", "denied", "not_evaluable"]


class DocumentationItem(Contract):
    work_id: Identifier
    status: Literal["documented", "insufficient_context", "unsupported", "preserved"]
    description_draft: str | None = Field(default=None, max_length=16000)
    evidence_ids: References
    supported_facts: list[ShortText] = Field(max_length=64)
    reason_code: ShortText | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "DocumentationItem":
        if self.status == "documented":
            if not self.description_draft or not self.description_draft.strip():
                raise ValueError("Documented items require text")
            if not self.evidence_ids or not self.supported_facts:
                raise ValueError("Documented items require evidence and supported facts")
        elif self.description_draft is not None or not self.reason_code:
            raise ValueError("Abstentions require a reason and must not contain a draft")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("Duplicate evidence references")
        return self


class DocumentationEnvelope(Contract):
    """Validate the batch boundary before independently validating work results."""

    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    batch_ref: Identifier
    items: list[dict[str, Any]] = Field(max_length=128)


class DocumentationResult(Contract):
    batch_ref: Identifier
    items: list[DocumentationItem] = Field(max_length=128)

    @model_validator(mode="after")
    def unique_work(self) -> "DocumentationResult":
        identifiers = [item.work_id for item in self.items]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Duplicate documentation work")
        return self


class DocumentReview(Contract):
    work_id: Identifier
    verdict: Literal["supported", "insufficient", "rejected"]
    unsupported_claims: list[ShortText] = Field(max_length=64)
    evidence_ids: References
    reason_code: ShortText | None


class DocumentReviews(Contract):
    batch_ref: Identifier
    reviews: list[DocumentReview] = Field(max_length=16)

    @model_validator(mode="after")
    def unique_reviews(self) -> "DocumentReviews":
        ids = [review.work_id for review in self.reviews]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate documentary review")
        return self


class DocumentReviewEnvelope(Contract):
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    batch_ref: Identifier
    reviews: list[dict[str, Any]] = Field(max_length=16)
