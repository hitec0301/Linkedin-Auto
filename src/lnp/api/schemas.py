"""Request and response shapes.

Written out rather than serialising the ORM directly, so what leaves the
server is a decision instead of a side effect. Nothing here carries a token, a
client secret, or another tenant's id.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from ..models import COLUMNS, Row


class RowOut(BaseModel):
    id: str = ""
    created_at: str = ""
    source_url: str = ""
    source_title: str = ""
    audience: str = ""
    theme: str = ""
    why_it_matters: str = ""
    relevance_score: float = 0.0
    selected: bool = False
    angle: str = ""
    draft_text: str = ""
    final_text: str = ""
    revision_note: str = ""
    revision_count: int = 0
    char_count: int = 0
    status: str = ""
    scheduled_for: str = ""
    post_urn: str = ""
    posted_at: str = ""
    edit_distance: float = 0.0
    reach: int = 0
    error: str = ""
    # What the interface is allowed to offer on this row, worked out from the
    # state machine rather than re-derived in the browser. Two copies of a
    # rule is one copy too many.
    allowed_actions: List[str] = Field(default_factory=list)

    @classmethod
    def of(cls, row: Row, allowed: List[str]) -> "RowOut":
        return cls(
            id=row.ID,
            created_at=row.CreatedAt,
            source_url=row.SourceURL,
            source_title=row.SourceTitle,
            audience=row.Audience,
            theme=row.Theme,
            why_it_matters=row.WhyItMatters,
            relevance_score=row.relevance_score,
            selected=row.is_selected,
            angle=row.Angle,
            draft_text=row.DraftText,
            final_text=row.FinalText,
            revision_note=row.RevisionNote,
            revision_count=row.revision_count,
            char_count=int(float(row.CharCount or 0)),
            status=row.status,
            scheduled_for=row.ScheduledFor,
            post_urn=row.PostURN,
            posted_at=row.PostedAt,
            edit_distance=float(row.EditDistance or 0),
            reach=int(float(row.Reach or 0)),
            error=row.Error,
            allowed_actions=allowed,
        )


class RowEdit(BaseModel):
    """The columns a person may change. Deliberately short."""

    selected: Optional[bool] = None
    angle: Optional[str] = None
    final_text: Optional[str] = None
    reach: Optional[int] = None


class ReviseIn(BaseModel):
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("note")
    @classmethod
    def not_only_whitespace(cls, value: str) -> str:
        """A blank note is not an instruction.

        Sending a draft back with nothing to act on wastes a model call and
        gives the human the same text again, which reads like the tool ignored
        them.
        """
        if not value.strip():
            raise ValueError("write what you want changed")
        return value.strip()


class MeOut(BaseModel):
    id: str
    email: str = ""
    name: str = ""
    picture_url: str = ""
    status: str = ""
    plan: str = ""
    timezone: str = ""
    paused: bool = True
    onboarded: bool = False
    linkedin_connected: bool = False
    linkedin_app_configured: bool = False
    post_count: int = 0


class UsageOut(BaseModel):
    period: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cap: int
    fraction_used: float
    cost_usd: float


class HealthOut(BaseModel):
    published: int
    clean: int
    clean_rate: float
    mean_edit_distance: float
    mean_edit_distance_first: float
    mean_edit_distance_last: float
    days_running: float
    verdict: str
    improving: Optional[bool] = None


class SourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    tier: int = Field(default=2, ge=1, le=3)
    audience: str = ""
    active: bool = True


class SourceOut(SourceIn):
    id: str
    last_ok_at: str = ""
    last_error: str = ""


class VoiceCardIn(BaseModel):
    content: str = Field(min_length=1)


class VoiceCardOut(BaseModel):
    content: str
    updated_at: str = ""


class AmendmentOut(BaseModel):
    id: str
    created_at: str = ""
    rule: str = ""
    rationale: str = ""
    signal: str = ""
    occurrences: int = 0
    recurring: bool = False
    accepted: bool = False
    applied: str = ""
    source_row_ids: List[str] = Field(default_factory=list)


class AmendmentDecision(BaseModel):
    accepted: bool


class LinkedInAppIn(BaseModel):
    client_id: str = Field(min_length=6, max_length=100)
    client_secret: str = Field(min_length=6, max_length=200)


class PauseIn(BaseModel):
    paused: bool


class SettingsIn(BaseModel):
    timezone: Optional[str] = None


ROW_COLUMN_BY_FIELD = {
    "selected": "Selected",
    "angle": "Angle",
    "final_text": "FinalText",
    "reach": "Reach",
}
assert set(ROW_COLUMN_BY_FIELD.values()) <= set(COLUMNS)
