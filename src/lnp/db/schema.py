"""The multi-tenant schema.

Every table that holds tenant data carries `tenant_id` and is indexed on it.
There is no table a query can reach without naming a tenant, and no code path
outside `PipelineStore` that builds a query at all — which is how a
multi-tenant product avoids the failure where one customer sees another's
drafts.

Ids are ULIDs stored as text, the same ids the pipeline already puts in the ID
column. They sort by creation time, so "oldest first" is an index scan rather
than a sort, and a row id means the same thing in the database, in the API and
in a log line.

Types are deliberately portable: the tests run this schema on SQLite so they
need no server, and it runs on Postgres in production. Nothing here uses a
dialect-specific type.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .crypto import Encrypted


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator):
    """A timestamp that is always timezone-aware UTC on the way out.

    Postgres hands back an aware datetime and SQLite hands back a naive one.
    Left alone, that difference only shows up as a TypeError comparing the two,
    in whichever code path the tests happened not to cover. Normalising here
    means the storage engine cannot change what a timestamp *is*.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


ID = String(26)  # a ULID


class Tenant(Base):
    """One paying customer, and the state that decides whether jobs run.

    `paused` is not stored here — it lives in Setting, alongside the other
    values the human toggles, because the kill switch has to be readable by the
    publish job through exactly one path.
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_now)
    email: Mapped[str] = mapped_column(String(320), default="")
    name: Mapped[str] = mapped_column(String(200), default="")
    # The `sub` claim from Sign in with LinkedIn: stable, and the only identity
    # we can rely on across a change of email. NULL until they sign in, and
    # NULL rather than "" because two tenants mid-signup must not collide on
    # the uniqueness constraint that keeps two accounts off one LinkedIn.
    linkedin_sub: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    picture_url: Mapped[str] = mapped_column(Text, default="")
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")

    # Billing. `status` gates the jobs: only `trialing` and `active` run.
    status: Mapped[str] = mapped_column(String(20), default="trialing", index=True)
    plan: Mapped[str] = mapped_column(String(40), default="standard")
    trial_ends_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    canceled_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)

    # The operator pays for model usage, so the cap is per tenant and enforced
    # before the call, not discovered on the invoice.
    monthly_token_cap: Mapped[int] = mapped_column(Integer, default=2_000_000)

    onboarded_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (UniqueConstraint("linkedin_sub", name="uq_tenants_linkedin_sub"),)

    @property
    def is_runnable(self) -> bool:
        return self.status in {"trialing", "active"}


class LinkedInApp(Base):
    """The tenant's own LinkedIn developer app.

    One app per tenant rather than one app for the product: LinkedIn's rate
    limits are per app, and a shared app makes every customer share one
    ceiling and one suspension. The cost is a setup step, which the onboarding
    setup screen walks them through.
    """

    __tablename__ = "linkedin_apps"

    tenant_id: Mapped[str] = mapped_column(
        ID, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    client_id: Mapped[str] = mapped_column(String(100), default="")
    client_secret: Mapped[str] = mapped_column(Encrypted, default="")
    redirect_uri: Mapped[str] = mapped_column(Text, default="")
    verified_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=_now, onupdate=_now
    )


class LinkedInToken(Base):
    """The tenant's OAuth tokens, encrypted at rest.

    Mirrors `tokens.TokenSet` field for field so the rotation logic that
    already exists works unchanged against this row.
    """

    __tablename__ = "linkedin_tokens"

    tenant_id: Mapped[str] = mapped_column(
        ID, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    access_token: Mapped[str] = mapped_column(Encrypted, default="")
    refresh_token: Mapped[str] = mapped_column(Encrypted, default="")
    expires_at: Mapped[str] = mapped_column(String(40), default="")
    refresh_expires_at: Mapped[str] = mapped_column(String(40), default="")
    person_urn: Mapped[str] = mapped_column(String(120), default="")
    obtained_at: Mapped[str] = mapped_column(String(40), default="")
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=_now, onupdate=_now
    )


class PipelineRow(Base):
    """One candidate item, from ingestion to published post.

    `archived_at` retires a row rather than deleting it: it stops appearing in
    the pipeline, and it stays, because the archive is what dedupe checks
    against and what the health metric is computed from.
    """

    __tablename__ = "pipeline_rows"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ID, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    source_url: Mapped[str] = mapped_column(Text, default="")
    source_title: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(String(40), default="")
    theme: Mapped[str] = mapped_column(String(40), default="")
    why_it_matters: Mapped[str] = mapped_column(Text, default="")
    relevance_score: Mapped[Optional[float]] = mapped_column(Float)
    selected: Mapped[Optional[bool]] = mapped_column(Boolean)
    angle: Mapped[str] = mapped_column(Text, default="")
    draft_text: Mapped[str] = mapped_column(Text, default="")
    final_text: Mapped[str] = mapped_column(Text, default="")
    revision_note: Mapped[str] = mapped_column(Text, default="")
    revision_count: Mapped[Optional[int]] = mapped_column(Integer)
    char_count: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="NEW", index=True)
    scheduled_for: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    post_urn: Mapped[str] = mapped_column(String(200), default="")
    posted_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    edit_distance: Mapped[Optional[float]] = mapped_column(Float)
    reach: Mapped[Optional[int]] = mapped_column(Integer)
    error: Mapped[str] = mapped_column(Text, default="")

    archived_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=_now, onupdate=_now
    )

    __table_args__ = (
        Index("ix_rows_tenant_status", "tenant_id", "status"),
        Index("ix_rows_tenant_archived", "tenant_id", "archived_at"),
    )


class Setting(Base):
    """The Config tab: the handful of values a human toggles."""

    __tablename__ = "settings"

    tenant_id: Mapped[str] = mapped_column(
        ID, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=_now, onupdate=_now
    )


class Feedback(Base):
    """One correction the human made — a note they wrote, or an edit they made."""

    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ID, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_now)
    row_id: Mapped[str] = mapped_column(ID, default="")
    signal: Mapped[str] = mapped_column(String(10), default="")
    instruction: Mapped[str] = mapped_column(Text, default="")
    draft_text: Mapped[str] = mapped_column(Text, default="")
    final_text: Mapped[str] = mapped_column(Text, default="")
    processed: Mapped[str] = mapped_column(String(40), default="")


class VoiceAmendment(Base):
    """A proposed voice rule.

    `accepted` is the human's tick. Nothing reaches the voice card without it,
    which is why it is a column the model's code path never writes.
    """

    __tablename__ = "voice_amendments"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ID, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_now)
    rule: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    signal: Mapped[str] = mapped_column(String(10), default="")
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    applied: Mapped[str] = mapped_column(String(80), default="")
    source_row_ids: Mapped[list] = mapped_column(JSON, default=list)


class VoiceCard(Base):
    """The tenant's voice card.

    A single markdown document, hand-edited, versioned only by `updated_at`.
    It stays one editable text rather than becoming structured fields, because
    the property that matters is that a human can fix a bad draft by rewriting
    a sentence in thirty seconds.
    """

    __tablename__ = "voice_cards"

    tenant_id: Mapped[str] = mapped_column(
        ID, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    content: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=_now, onupdate=_now
    )


class Source(Base):
    """One feed the curate job reads for this tenant."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ID, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    tier: Mapped[int] = mapped_column(Integer, default=2)
    audience: Mapped[str] = mapped_column(String(40), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_ok_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    last_error: Mapped[str] = mapped_column(Text, default="")


class UsageEvent(Base):
    """One model call, recorded for the cap.

    The operator pays for inference, so usage is metered per tenant and per
    call. Append-only: a cap that is computed from a running counter drifts,
    and a drifting counter is either a customer cut off early or a bill nobody
    budgeted for.
    """

    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ID, ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_now)
    period: Mapped[str] = mapped_column(String(7), default="", index=True)  # YYYY-MM
    job: Mapped[str] = mapped_column(String(40), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0)  # millionths of USD

    __table_args__ = (Index("ix_usage_tenant_period", "tenant_id", "period"),)


ALL_TABLES = [
    Tenant, LinkedInApp, LinkedInToken, PipelineRow, Setting,
    Feedback, VoiceAmendment, VoiceCard, Source, UsageEvent,
]
