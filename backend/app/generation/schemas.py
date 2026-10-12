from typing import Literal
from pydantic import BaseModel, Field


class Finding(BaseModel):
    id: str
    category: str
    severity: Literal["critical", "error", "warning"]
    quote: str
    explanation: str
    suggestion: str
    rule_id: str
    status: Literal["open", "fixed", "rejected"] = "open"


class PlanResult(BaseModel):
    scenes: list[str]
    chapter_change: str
    unresolved_questions: list[str]


class TextResult(BaseModel):
    text: str = Field(min_length=1)
    proposals: list[str] = Field(default_factory=list)


class ReviewResult(BaseModel):
    findings: list[Finding]
    complete: bool
    limitations: list[str]


class JobCreate(BaseModel):
    chapter_id: str
    base_revision_id: str | None = None
    kind: Literal["write", "review"] = "write"
    instructions: str = Field(default="", max_length=12000)
    max_review_passes: int = Field(default=3, ge=1, le=10)
    commercial_report: bool = False
    budget_usd: float | None = Field(default=None, gt=0, le=100)


class ApplyJob(BaseModel):
    base_revision_id: str | None = None
    mutation_id: str = Field(min_length=8, max_length=100)
    acknowledge_critical: bool = False


class PromptUpdate(BaseModel):
    text: str = Field(min_length=100, max_length=20000)
    base_version: int = Field(ge=0)
