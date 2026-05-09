from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field


class TickerInfo(BaseModel):
    ticker: str
    company_name: str
    confidence: Literal["high", "medium", "low"]


class Risk(BaseModel):
    id: str
    label: str
    summary: str
    raw_excerpt: str


class ExtractedRisks(BaseModel):
    latest_year_risks: list[Risk]
    prior_year_risks: list[Risk]


class DiffItem(BaseModel):
    verdict: Literal["added", "removed", "materially_changed", "unchanged"]
    latest_risk_id: str | None = None
    prior_risk_id: str | None = None
    rationale: str


class SummaryCounts(BaseModel):
    added: int = 0
    removed: int = 0
    materially_changed: int = 0
    unchanged: int = 0


class RiskDiff(BaseModel):
    items: list[DiffItem]
    summary_counts: SummaryCounts = Field(default_factory=SummaryCounts)


class ConfidenceAssessment(BaseModel):
    risk_id: str
    confidence: Literal["High", "Medium", "Low"]
    justification: str


class AgentState(BaseModel):
    user_input: str
    ticker_info: TickerInfo | None = None
    edgar: dict | None = None
    extracted_risks: ExtractedRisks | None = None
    risk_diff: RiskDiff | None = None
    news_evidence: dict[str, list] | None = None
    final_memo_md: str | None = None
    confidence_assessments: list[dict] | None = None
    errors: list[str] = Field(default_factory=list)
    step_durations_ms: dict[str, int] = Field(default_factory=dict)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def to_json(self, **kwargs) -> str:
        return self.model_dump_json(indent=2, **kwargs)
