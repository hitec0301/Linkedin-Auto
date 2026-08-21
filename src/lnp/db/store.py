"""The Postgres implementation of the storage port.

This is the only module in the product that writes SQL. Every query filters on
`tenant_id`, and the tenant id comes from the store object rather than from an
argument, so there is no call site that can forget it.

The Sheet's columns become typed columns here, which is the one place the two
adapters genuinely differ: a Sheet cell is a string and a database column is
not. `FIELDS` is that mapping, written once, and used in both directions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import ulid
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import log
from ..models import COLUMNS, Row, Status
from ..store import (
    KEY_POST_COUNT,
    AmendmentRecord,
    FeedbackRecord,
    PipelineStore,
    StoreError,
)
from ..util import iso, parse_bool, parse_dt, parse_float, parse_int, utcnow
from .schema import (
    Feedback,
    PipelineRow,
    Setting,
    Tenant,
    VoiceAmendment,
    VoiceCard,
)

logger = log.get(__name__)

# (Sheet column, ORM attribute, kind). The order is COLUMNS' order, and the
# test suite asserts every column appears exactly once — a column added to the
# model without a mapping here would otherwise be silently dropped on write.
FIELDS: List[Tuple[str, str, str]] = [
    ("ID", "id", "text"),
    ("CreatedAt", "created_at", "ts"),
    ("SourceURL", "source_url", "text"),
    ("SourceTitle", "source_title", "text"),
    ("Audience", "audience", "text"),
    ("Theme", "theme", "text"),
    ("WhyItMatters", "why_it_matters", "text"),
    ("RelevanceScore", "relevance_score", "float"),
    ("Selected", "selected", "bool"),
    ("Angle", "angle", "text"),
    ("DraftText", "draft_text", "text"),
    ("FinalText", "final_text", "text"),
    ("RevisionNote", "revision_note", "text"),
    ("RevisionCount", "revision_count", "int"),
    ("CharCount", "char_count", "int"),
    ("Status", "status", "text"),
    ("ScheduledFor", "scheduled_for", "ts"),
    ("PostURN", "post_urn", "text"),
    ("PostedAt", "posted_at", "ts"),
    ("EditDistance", "edit_distance", "float"),
    ("Reach", "reach", "int"),
    ("Error", "error", "text"),
]

BY_COLUMN: Dict[str, Tuple[str, str]] = {c: (a, k) for c, a, k in FIELDS}


def to_db(value: Any, kind: str) -> Any:
    """A Sheet-shaped string into a typed column value.

    Blank means *unset*, not zero: a row that has never been scored has no
    RelevanceScore, and storing 0.0 would make it look like a scored one that
    scored nothing.
    """
    text = "" if value is None else str(value).strip()
    if kind == "text":
        return text
    if text == "":
        return None
    if kind == "ts":
        return parse_dt(text)
    if kind == "int":
        return parse_int(text, 0)
    if kind == "float":
        return parse_float(text, 0.0)
    if kind == "bool":
        return parse_bool(text)
    raise StoreError(f"unknown field kind {kind!r}")


def from_db(value: Any, kind: str) -> str:
    """A typed column value back into what the rest of the pipeline expects."""
    if value is None:
        return ""
    if kind == "ts":
        return iso(value) if isinstance(value, datetime) else str(value)
    if kind == "bool":
        return "TRUE" if value else "FALSE"
    if kind == "float":
        return f"{float(value):g}"
    return str(value)


def to_row(orm: PipelineRow) -> Row:
    return Row(**{col: from_db(getattr(orm, attr), kind) for col, attr, kind in FIELDS})


class PostgresStore(PipelineStore):
    """One tenant's pipeline, in the database."""

    def __init__(self, session: Session, tenant_id: str, config=None):
        self.session = session
        self.tenant_id = tenant_id
        self.config = config

    @classmethod
    def for_tenant(cls, tenant_id: str, config=None) -> "PostgresStore":
        from .session import session_factory

        return cls(session_factory()(), tenant_id, config)

    # ---- rows ------------------------------------------------------------

    def _rows_query(self, include_archived: bool = False):
        stmt = select(PipelineRow).where(PipelineRow.tenant_id == self.tenant_id)
        if not include_archived:
            stmt = stmt.where(PipelineRow.archived_at.is_(None))
        return stmt.order_by(PipelineRow.id)

    def pipeline_rows(self) -> List[Row]:
        return [to_row(r) for r in self.session.scalars(self._rows_query()).all()]

    def _orm_row(self, row: Row) -> PipelineRow:
        if not row.ID:
            raise StoreError("row has no ID; cannot write it")
        found = self.session.get(PipelineRow, row.ID)
        if found is None or found.tenant_id != self.tenant_id:
            # Not "not found": a row id that belongs to another tenant must
            # read exactly the same as one that does not exist.
            raise StoreError(f"row {row.ID} does not exist")
        return found

    def append_rows(self, rows: Iterable[Row]) -> int:
        count = 0
        for row in rows:
            row.touch_created()
            if not row.ID:
                row.ID = ulid.new().str
            orm = PipelineRow(tenant_id=self.tenant_id)
            for col, attr, kind in FIELDS:
                setattr(orm, attr, to_db(getattr(row, col), kind))
            if not orm.status:
                orm.status = Status.NEW
            self.session.add(orm)
            count += 1
        self.session.commit()
        if count:
            logger.info("appended pipeline rows", extra={"count": count})
        return count

    def _write_fields(self, row: Row, updates: Dict[str, Any], *, allow_revision_note: bool) -> None:
        orm = self._orm_row(row)
        for name, value in updates.items():
            attr, kind = BY_COLUMN[name]
            setattr(orm, attr, to_db(value, kind))
        self.session.commit()
        logger.info("row updated", extra={"row_id": row.ID, "fields": list(updates)})

    def recent_index(self, days: int) -> Tuple[List[str], List[str]]:
        """URLs and titles seen in the trailing window, archive included.

        Dedupe reads this, so archived rows have to be in it: a story that ran
        three weeks ago should not come back because a second outlet picked it
        up and the first row has since been retired.
        """
        cutoff = utcnow() - timedelta(days=days)
        stmt = (
            select(PipelineRow.source_url, PipelineRow.source_title)
            .where(PipelineRow.tenant_id == self.tenant_id)
            .where(
                (PipelineRow.created_at.is_(None)) | (PipelineRow.created_at >= cutoff)
            )
        )
        urls: List[str] = []
        titles: List[str] = []
        for url, title in self.session.execute(stmt):
            if url:
                urls.append(url)
            if title:
                titles.append(title)
        return urls, titles

    def archive_old_rows(self, days: int) -> int:
        """Retire finished rows past the window. Nothing is deleted."""
        cutoff = utcnow() - timedelta(days=days)
        stmt = (
            self._rows_query()
            .where(PipelineRow.status.in_([Status.POSTED, Status.SKIPPED, Status.EXPIRED]))
            .where(PipelineRow.created_at < cutoff)
        )
        stale = list(self.session.scalars(stmt))
        now = utcnow()
        for orm in stale:
            orm.archived_at = now
        self.session.commit()
        if stale:
            logger.info("archived rows", extra={"count": len(stale)})
        return len(stale)

    # ---- settings --------------------------------------------------------

    def config_values(self) -> Dict[str, str]:
        stmt = select(Setting).where(Setting.tenant_id == self.tenant_id)
        return {s.key.upper(): s.value for s in self.session.scalars(stmt)}

    def set_config_value(self, key: str, value: str) -> None:
        key = key.upper()
        found = self.session.get(Setting, (self.tenant_id, key))
        if found is None:
            self.session.add(
                Setting(tenant_id=self.tenant_id, key=key, value=str(value))
            )
        else:
            found.value = str(value)
        self.session.commit()

    def bump_post_count(self) -> int:
        """Increment in the database rather than read-modify-write in Python.

        Two publish runs overlapping is rare but not impossible, and a lost
        increment silently changes drafting behaviour: the post count is what
        decides whether the model gets few-shot examples.
        """
        found = self.session.get(Setting, (self.tenant_id, KEY_POST_COUNT))
        if found is None:
            found = Setting(tenant_id=self.tenant_id, key=KEY_POST_COUNT, value="0")
            self.session.add(found)
        try:
            count = int(float(found.value or 0)) + 1
        except ValueError:
            count = 1
        found.value = str(count)
        self.session.commit()
        return count

    # ---- feedback --------------------------------------------------------

    def feedback_records(self) -> List[FeedbackRecord]:
        stmt = (
            select(Feedback)
            .where(Feedback.tenant_id == self.tenant_id)
            .order_by(Feedback.id)
        )
        return [
            FeedbackRecord(
                id=f.id,
                created_at=iso(f.created_at) if f.created_at else "",
                row_id=f.row_id,
                signal=f.signal,
                instruction=f.instruction,
                draft_text=f.draft_text,
                final_text=f.final_text,
                processed=f.processed,
                ref=f.id,
            )
            for f in self.session.scalars(stmt)
        ]

    def append_feedback(self, records: Sequence[FeedbackRecord]) -> int:
        for r in records:
            self.session.add(
                Feedback(
                    id=r.id or ulid.new().str,
                    tenant_id=self.tenant_id,
                    created_at=parse_dt(r.created_at) or utcnow(),
                    row_id=r.row_id,
                    signal=r.signal,
                    instruction=r.instruction,
                    draft_text=r.draft_text,
                    final_text=r.final_text,
                    processed=r.processed,
                )
            )
        self.session.commit()
        return len(records)

    # ---- amendments ------------------------------------------------------

    def amendment_records(self) -> List[AmendmentRecord]:
        stmt = (
            select(VoiceAmendment)
            .where(VoiceAmendment.tenant_id == self.tenant_id)
            .order_by(VoiceAmendment.id)
        )
        return [
            AmendmentRecord(
                id=a.id,
                created_at=iso(a.created_at) if a.created_at else "",
                rule=a.rule,
                rationale=a.rationale,
                signal=a.signal,
                occurrences=a.occurrences or 0,
                recurring=bool(a.recurring),
                accepted=bool(a.accepted),
                applied=a.applied or "",
                source_row_ids=list(a.source_row_ids or []),
                ref=a.id,
            )
            for a in self.session.scalars(stmt)
        ]

    def append_amendments(self, records: Sequence[AmendmentRecord]) -> int:
        for r in records:
            self.session.add(
                VoiceAmendment(
                    id=r.id or ulid.new().str,
                    tenant_id=self.tenant_id,
                    created_at=parse_dt(r.created_at) or utcnow(),
                    rule=r.rule,
                    rationale=r.rationale,
                    signal=r.signal,
                    occurrences=r.occurrences,
                    recurring=r.recurring,
                    accepted=r.accepted,
                    applied=r.applied,
                    source_row_ids=list(r.source_row_ids),
                )
            )
        self.session.commit()
        return len(records)

    def mark_amendment_applied(self, record: AmendmentRecord, value: str) -> None:
        found = self.session.get(VoiceAmendment, record.ref or record.id)
        if found is None or found.tenant_id != self.tenant_id:
            raise StoreError(f"amendment {record.id} does not exist")
        found.applied = value
        self.session.commit()

    # ---- voice card ------------------------------------------------------

    def load_voice_card(self) -> str:
        found = self.session.get(VoiceCard, self.tenant_id)
        if found is None or not (found.content or "").strip():
            raise StoreError(
                "this account has no voice card yet, and the pipeline will not "
                "draft without one. Finish onboarding first."
            )
        return found.content

    def save_voice_card(self, text: str) -> None:
        found = self.session.get(VoiceCard, self.tenant_id)
        if found is None:
            self.session.add(VoiceCard(tenant_id=self.tenant_id, content=text))
        else:
            found.content = text
        self.session.commit()

    # ---- tenant ----------------------------------------------------------

    def tenant(self) -> Tenant:
        found = self.session.get(Tenant, self.tenant_id)
        if found is None:
            raise StoreError(f"tenant {self.tenant_id} does not exist")
        return found

    def close(self) -> None:
        self.session.close()
