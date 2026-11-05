"""Model outputs propose decisions; these types grant no permissions or effects."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Short = Annotated[str, Field(min_length=1, max_length=500)]

Reason = Annotated[str, Field(max_length=2000)]

Keys = Annotated[list[Short], Field(max_length=50)]

ROLLING_DAYS_PATTERN = r'last_([1-9]|[12][0-9]|3[01])_days'

class Output(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class Finding(Output):
    claim: Reason
    kind: Literal['observation', 'opinion', 'inference', 'hypothesis']
    supports: Keys
    refutes: Keys
    limitations: Annotated[list[Reason], Field(max_length=10)]

class FactualIssue(Output):
    claim: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_id: Short
    quoted_text: Annotated[str, Field(min_length=1, max_length=2000)]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]

class FactualReview(Output):
    accepted: bool
    issues: Annotated[list[FactualIssue], Field(max_length=10)]

    @model_validator(mode='after')
    def consistent_verdict(self):
        if self.accepted != (not self.issues):
            raise ValueError('Factual verdict must agree with its issues')
        return self

class FinancialPoint(Output):
    period: Annotated[str, Field(pattern=r'^\d{4}(?:-\d{2}-\d{2})?$')]
    value: Annotated[float, Field(allow_inf_nan=False)] | None

class FinancialMetrics(Output):
    metric: Literal['close', 'value']
    entity: Short
    indicator: Short | None
    unit: Short
    currency: Short | None
    points: Annotated[list[FinancialPoint], Field(min_length=1, max_length=50)]
