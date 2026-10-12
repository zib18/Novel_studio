from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Kinds = Literal["core", "character", "relationship", "timeline", "knowledge", "plot_thread",
                "foreshadowing", "future_vault", "current_state", "rejected", "idea", "plan"]
ApprovedStatus = Literal["CANON", "FACT", "FUTURE_CANON"]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NovelSettings(InputModel):
    target_min_chars: int = Field(default=6000, ge=500, le=100000)
    target_max_chars: int = Field(default=9000, ge=500, le=100000)
    max_review_passes: int = Field(default=3, ge=1, le=10)
    job_budget_usd: float = Field(default=1.0, gt=0, le=100)
    pace: Literal["balanced", "fast", "slow"] = "balanced"

    @model_validator(mode="after")
    def range_order(self):
        if self.target_min_chars > self.target_max_chars:
            raise ValueError("Минимальный объём не может превышать максимальный")
        return self


class NovelSettingsPatch(InputModel):
    target_min_chars: int | None = Field(default=None, ge=500, le=100000)
    target_max_chars: int | None = Field(default=None, ge=500, le=100000)
    max_review_passes: int | None = Field(default=None, ge=1, le=10)
    job_budget_usd: float | None = Field(default=None, gt=0, le=100)
    pace: Literal["balanced", "fast", "slow"] | None = None


class Login(InputModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class NovelCreate(InputModel):
    title: str = Field(min_length=1, max_length=240)
    genre: str = Field(default="", max_length=240)
    concept: str = Field(default="", max_length=100000)
    settings: NovelSettings = Field(default_factory=NovelSettings)


class NovelPatch(InputModel):
    base_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    genre: str | None = Field(default=None, max_length=240)
    concept: str | None = Field(default=None, max_length=100000)
    settings: NovelSettingsPatch | None = None


class ChapterCreate(InputModel):
    title: str = Field(default="", max_length=240)
    number: int | None = Field(default=None, ge=1, le=100000)


class SaveText(InputModel):
    base_revision_id: str
    text: str = Field(max_length=500000)
    mutation_id: str = Field(min_length=8, max_length=128)


class Restore(InputModel):
    base_revision_id: str
    revision_id: str
    mutation_id: str = Field(min_length=8, max_length=128)


class ChapterApproval(InputModel):
    base_revision_id: str


class BibleCreate(InputModel):
    kind: Kinds
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(default="", max_length=100000)
    status: Literal["IDEA", "PROPOSAL", "UNDEFINED"] = "IDEA"
    available_from_chapter: int | None = Field(default=None, ge=1)
    details: dict = Field(default_factory=dict)


class BibleDecision(InputModel):
    action: Literal["approve", "reject"]
    base_version: int = Field(ge=1)
    expected_canon_version: int = Field(ge=1)
    target_status: ApprovedStatus = "CANON"
    reason: str = Field(default="", max_length=10000)


class BibleReplacement(InputModel):
    base_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=240)
    content: str = Field(max_length=100000)
    details: dict | None = None
    available_from_chapter: int | None = Field(default=None, ge=1)


class RejectionCreate(InputModel):
    phrase: str = Field(default="", max_length=10000)
    description: str = Field(min_length=1, max_length=10000)
    scope: str | None = None
    severity: Literal["warning", "major", "critical"] = "critical"
