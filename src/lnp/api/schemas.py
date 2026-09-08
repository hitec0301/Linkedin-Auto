"""Request and response shapes.

Written out rather than serialising the ORM directly, so what leaves the
server is a decision instead of a side effect. Nothing here carries a token, a
client secret, or another tenant's id.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from ..drafting import contains_both_variants, split_variants
from ..models import ALL_STATUSES, COLUMNS, Row


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
    # The angle before a draft exists, the revision instruction once it does -
    # one field the interface shows in one place for the row's whole life.
    take: str = ""
    draft_text: str = ""
    final_text: str = ""
    # Set only while DraftText still holds two undecided variants (the first
    # twenty posts draft two, to show the range). variant_b non-empty is the
    # signal the interface uses to show the picker instead of one post body -
    # once a variant is chosen into FinalText, these go back to blank.
    variant_a: str = ""
    variant_b: str = ""
    revision_count: int = 0
    char_count: int = 0
    status: str = ""
    scheduled_for: str = ""
    post_urn: str = ""
    posted_at: str = ""
    edit_distance: float = 0.0
    reach: int = 0
    error: str = ""
    image_prompt: str = ""
    # A data: URI, ready for an <img src>, rather than raw base64 - the
    # frontend never needs to know the encoding is base64 or the mime type.
    image_data_url: str = ""

    @classmethod
    def of(cls, row: Row) -> "RowOut":
        variant_a, variant_b = "", ""
        if not (row.FinalText or "").strip() and contains_both_variants(row.DraftText):
            variant_a, second = split_variants(row.DraftText)
            variant_b = second or ""
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
            take=row.Angle if not row.DraftText else (row.RevisionNote or row.Angle),
            draft_text=row.DraftText,
            final_text=row.FinalText,
            variant_a=variant_a,
            variant_b=variant_b,
            revision_count=row.revision_count,
            char_count=int(float(row.CharCount or 0)),
            status=row.status,
            scheduled_for=row.ScheduledFor,
            post_urn=row.PostURN,
            posted_at=row.PostedAt,
            edit_distance=float(row.EditDistance or 0),
            reach=int(float(row.Reach or 0)),
            error=row.Error,
            image_prompt=row.ImagePrompt,
            image_data_url=f"data:image/png;base64,{row.ImageData}" if row.ImageData else "",
        )


class RowEdit(BaseModel):
    """The columns a person may change. Deliberately short."""

    selected: Optional[bool] = None
    take: Optional[str] = None
    final_text: Optional[str] = None
    reach: Optional[int] = None


class StatusIn(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def a_real_status(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in ALL_STATUSES:
            raise ValueError(f"{value!r} is not a status this pipeline has")
        return value


class RedraftIn(BaseModel):
    """The take, sent along with the redraft request itself.

    Rather than relying on a prior autosave of the same field having
    already landed - the button and the field are right next to each
    other, and the request should carry exactly what is in the box when it
    is pressed, not whatever the last debounce happened to save.
    """

    take: str = ""


class BulkSkipIn(BaseModel):
    ids: List[str] = Field(min_length=1, max_length=200)


class NewPostIn(BaseModel):
    """A post someone starts themselves, instead of from a fetched candidate.

    `take` is the same field the card shows once the row exists - the thesis
    a first draft is built from - so a self-started row and a fetched one are
    indistinguishable the moment after creation.
    """

    source_url: str = Field(min_length=1, max_length=2000)
    source_title: str = Field(default="", max_length=300)
    take: str = Field(default="", max_length=20000)

    @field_validator("source_url")
    @classmethod
    def looks_like_a_url(cls, value: str) -> str:
        value = value.strip()
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("needs to be a full URL, starting with http:// or https://")
        return value


class ScheduleIn(BaseModel):
    scheduled_for: str = Field(min_length=1)


# --------------------------------------------------------------------------
# Discuss: explore a source, argue with it, before it becomes a post
# --------------------------------------------------------------------------


class NewDiscussionIn(BaseModel):
    source_url: str = Field(min_length=1, max_length=2000)
    source_title: str = Field(default="", max_length=300)
    row_id: str = Field(default="", max_length=26)

    @field_validator("source_url")
    @classmethod
    def looks_like_a_url(cls, value: str) -> str:
        value = value.strip()
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("needs to be a full URL, starting with http:// or https://")
        return value


class DiscussionMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000)

    @field_validator("content")
    @classmethod
    def not_only_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("say something to respond to")
        return value.strip()


class DiscussionMessageOut(BaseModel):
    role: str
    content: str


class DiscussionOut(BaseModel):
    id: str
    created_at: str = ""
    updated_at: str = ""
    source_url: str = ""
    source_title: str = ""
    started_from_row_id: str = ""
    row_id: str = ""
    messages: List[DiscussionMessageOut] = Field(default_factory=list)


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
    audience_description: str = ""


class AudienceIn(BaseModel):
    description: str = Field(min_length=1, max_length=4000)

    @field_validator("description")
    @classmethod
    def not_only_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("say something about who you write for")
        return value.strip()


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
    "final_text": "FinalText",
    "reach": "Reach",
}
assert set(ROW_COLUMN_BY_FIELD.values()) <= set(COLUMNS)
