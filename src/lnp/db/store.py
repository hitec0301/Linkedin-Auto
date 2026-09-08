"""One tenant's pipeline, in Postgres.

This is the only module in the product that writes SQL. Every query filters on
`tenant_id`, and the tenant id comes from the store object rather than from an
argument, so there is no call site that can forget which customer it is
serving. A row id belonging to another tenant reads exactly like one that does
not exist.

Two rules live in the write path rather than at the call sites, because a call
site is a place somebody can forget:

  * `write` refuses to touch a human's column,
  * `write_as_human` refuses to touch the model's,

and both refuse before anything is persisted. `transition` checks the status
machine on the same terms. Everything else in the class is plumbing.

`Row` is still a record of strings, a leftover from when this pipeline stored
its rows in a spreadsheet. `FIELDS` is where that meets real column types,
written once and used in both directions; retyping `Row` itself is worth doing
and is a change on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import ulid
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import log
from ..models import (
    COLUMNS,
    LEGAL_TRANSITIONS,
    Row,
    Status,
    assert_human_writable,
    assert_transition,
    assert_writable,
)
from ..util import iso, parse_bool, parse_dt, parse_float, parse_int, utcnow
from .schema import (
    Discussion,
    Feedback,
    PipelineRow,
    Setting,
    Tenant,
    VoiceAmendment,
    VoiceCard,
)

logger = log.get(__name__)

# Settings the pipeline reads back out of storage. PAUSED is the kill switch.
KEY_PAUSED = "PAUSED"
KEY_POST_COUNT = "POST_COUNT"
KEY_PERSON_URN = "PERSON_URN"
KEY_AUDIENCE_DESCRIPTION = "AUDIENCE_DESCRIPTION"

TRUTHY = {"true", "yes", "1", "on", "paused"}


class StoreError(Exception):
    pass


@dataclass
class FeedbackRecord:
    """One correction the human made, as stored."""

    id: str = ""
    created_at: str = ""
    row_id: str = ""
    signal: str = ""
    instruction: str = ""
    draft_text: str = ""
    final_text: str = ""
    processed: str = ""
    # Opaque handle back to the record this came from. Callers pass it back;
    # they never parse it.
    ref: str = ""


@dataclass
class AmendmentRecord:
    """One proposed voice rule, awaiting or carrying the human's decision."""

    id: str = ""
    created_at: str = ""
    rule: str = ""
    rationale: str = ""
    signal: str = ""
    occurrences: int = 0
    recurring: bool = False
    accepted: bool = False
    applied: str = ""
    source_row_ids: List[str] = field(default_factory=list)
    ref: str = ""

    @property
    def is_pending(self) -> bool:
        """Ticked by the human and not yet written into the card."""
        return self.accepted and not (self.applied or "").strip()


@dataclass
class DiscussionRecord:
    """A scratchpad conversation, as stored."""

    id: str = ""
    created_at: str = ""
    updated_at: str = ""
    source_url: str = ""
    source_title: str = ""
    started_from_row_id: str = ""
    row_id: str = ""
    messages: List[Dict[str, str]] = field(default_factory=list)


# (Row field, ORM attribute, kind). The order is COLUMNS' order, and the test
# suite asserts every column appears exactly once — a column added to the model
# without a mapping here would otherwise be silently dropped on write.
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
    ("ImagePrompt", "image_prompt", "text"),
    ("ImageData", "image_data", "text"),
]

BY_COLUMN: Dict[str, Tuple[str, str]] = {c: (a, k) for c, a, k in FIELDS}


def to_db(value: Any, kind: str) -> Any:
    """A Row's string into a typed column value.

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


class PipelineStore:
    """Everything the four jobs and the API do to one tenant's state."""

    def __init__(self, session: Session, tenant_id: str, config=None):
        self.session = session
        self.tenant_id = tenant_id
        self.config = config

    @classmethod
    def for_tenant(cls, tenant_id: str, config=None) -> "PipelineStore":
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

    def _write_fields(self, row: Row, updates: Dict[str, Any]) -> None:
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


    # ---- the guarded write path ------------------------------------------

    def write(
        self, row: Row, updates: Dict[str, Any], *,
        allow_revision_note: bool = False, allow_final_text_reset: bool = False,
    ) -> None:
        """A job writing to a row.

        Refuses the human's columns, and refuses before touching the database,
        so a rejected write leaves nothing half-applied.
        """
        if not updates:
            return
        assert_writable(
            list(updates),
            allow_revision_note=allow_revision_note,
            allow_final_text_reset=allow_final_text_reset,
        )
        self._write_fields(row, updates)
        for name, value in updates.items():
            setattr(row, name, "" if value is None else str(value))

    def write_as_human(self, row: Row, updates: Dict[str, Any]) -> None:
        """A person's edit, from the interface they use.

        The opposite guard: a job may not touch the human's columns, and the
        human's interface may not touch the model's. Both exist for the same
        reason - the gap between the draft and what was published is the
        measurement, and either side writing over the other destroys it.
        """
        if not updates:
            return
        assert_human_writable(list(updates))
        self._write_fields(row, updates)
        for name, value in updates.items():
            setattr(row, name, "" if value is None else str(value))

    def transition(
        self,
        row: Row,
        target: str,
        updates: Optional[Dict[str, Any]] = None,
        *,
        allow_revision_note: bool = False,
    ) -> None:
        """Move a row to `target`, writing any other columns in the same call.

        The move is checked before anything is written: an illegal transition
        leaves the database untouched.
        """
        assert_transition(row.status, target)
        payload: Dict[str, Any] = dict(updates or {})
        payload["Status"] = target
        self.write(row, payload, allow_revision_note=allow_revision_note)
        logger.info("status changed", extra={"row_id": row.ID, "to": target})

    def set_status_freely(self, row: Row, target: str) -> None:
        """Move a row directly to any status, bypassing the transition guard.

        For the one interface action deliberately not gated by the state
        machine: the status dropdown. A target that is not a real status is
        still refused, and POSTING is refused outright - it is not a status
        a person sets, it is the marker the publish path writes immediately
        before calling LinkedIn and clears immediately after, and setting it
        by hand is the one way to make an unpublished row look mid-flight.
        Everything else moves on request. An irregular jump - one the state
        machine itself would not have allowed - is logged rather than
        blocked, so the safety net removed from the interface is not also
        removed from the record of what happened.
        """
        if target == Status.POSTING:
            raise StoreError(
                "POSTING is a marker the publish job sets itself while a "
                "post is going out, not a status you can choose"
            )
        if target not in LEGAL_TRANSITIONS:
            raise StoreError(f"unknown status {target!r}")
        if target != row.status and target not in LEGAL_TRANSITIONS.get(row.status, set()):
            logger.warning(
                "status set outside the normal state machine",
                extra={"row_id": row.ID, "from": row.status, "to": target},
            )
        self.write(row, {"Status": target})
        logger.info("status changed (free choice)", extra={"row_id": row.ID, "to": target})

    def published_rows(self) -> List[Row]:
        return [r for r in self.pipeline_rows() if r.status == Status.POSTED]

    def expire_stale(
        self,
        rows: Sequence[Row],
        staleness_hours: int,
        now: Optional[datetime] = None,
    ) -> List[Row]:
        """Retire rows too far past their slot to be worth posting.

        A four-day-old take is worse than no post. Only DRAFTED and APPROVED
        rows can expire: a row stuck in POSTING is a different problem, and
        resolving it needs the LinkedIn API, not a clock.
        """
        expired: List[Row] = []
        for row in rows:
            if row.status not in {Status.DRAFTED, Status.APPROVED}:
                continue
            if not row.is_stale(staleness_hours, now=now):
                continue
            self.transition(
                row,
                Status.EXPIRED,
                {
                    "Error": (
                        f"expired: more than {staleness_hours}h past "
                        f"ScheduledFor ({row.ScheduledFor})"
                    )
                },
            )
            expired.append(row)
            logger.warning(
                "row expired",
                extra={"row_id": row.ID, "scheduled_for": row.ScheduledFor},
            )
        return expired

    def is_paused(self) -> bool:
        """The kill switch.

        A settings store that cannot be read counts as paused, and so does a
        missing key. If the customer's stop button is unreachable, the only
        safe reading is that it might be pressed.
        """
        try:
            values = self.config_values()
        except Exception:  # noqa: BLE001 - any storage failure means "unknown"
            logger.error("settings unreadable; treating pipeline as PAUSED")
            return True
        raw = values.get(KEY_PAUSED, "")
        if raw == "":
            logger.error("PAUSED is not set; treating pipeline as PAUSED")
            return True
        return str(raw).strip().lower() in TRUTHY

    def post_count(self) -> int:
        try:
            return int(float(self.config_values().get(KEY_POST_COUNT, "0") or 0))
        except (ValueError, StoreError):
            return 0

    def pending_amendments(self) -> List[AmendmentRecord]:
        return [r for r in self.amendment_records() if r.is_pending]

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

    # ---- discussions -------------------------------------------------------

    @staticmethod
    def _discussion_record(d: Discussion) -> DiscussionRecord:
        return DiscussionRecord(
            id=d.id,
            created_at=iso(d.created_at) if d.created_at else "",
            updated_at=iso(d.updated_at) if d.updated_at else "",
            source_url=d.source_url,
            source_title=d.source_title,
            started_from_row_id=d.started_from_row_id,
            row_id=d.row_id,
            messages=list(d.messages or []),
        )

    def discussions(self) -> List[DiscussionRecord]:
        stmt = (
            select(Discussion)
            .where(Discussion.tenant_id == self.tenant_id)
            .order_by(Discussion.updated_at.desc())
        )
        return [self._discussion_record(d) for d in self.session.scalars(stmt)]

    def discussion(self, discussion_id: str) -> DiscussionRecord:
        found = self.session.get(Discussion, discussion_id)
        if found is None or found.tenant_id != self.tenant_id:
            raise StoreError(f"discussion {discussion_id} does not exist")
        return self._discussion_record(found)

    def create_discussion(
        self, *, source_url: str, source_title: str, started_from_row_id: str,
        messages: Sequence[Dict[str, str]],
    ) -> DiscussionRecord:
        found = Discussion(
            id=ulid.new().str,
            tenant_id=self.tenant_id,
            source_url=source_url,
            source_title=source_title,
            started_from_row_id=started_from_row_id,
            messages=list(messages),
        )
        self.session.add(found)
        self.session.commit()
        return self._discussion_record(found)

    def append_discussion_messages(
        self, discussion_id: str, messages: Sequence[Dict[str, str]]
    ) -> DiscussionRecord:
        found = self.session.get(Discussion, discussion_id)
        if found is None or found.tenant_id != self.tenant_id:
            raise StoreError(f"discussion {discussion_id} does not exist")
        found.messages = list(found.messages or []) + list(messages)
        self.session.commit()
        return self._discussion_record(found)

    def mark_discussion_committed(self, discussion_id: str, row_id: str) -> None:
        found = self.session.get(Discussion, discussion_id)
        if found is None or found.tenant_id != self.tenant_id:
            raise StoreError(f"discussion {discussion_id} does not exist")
        found.row_id = row_id
        self.session.commit()

    def delete_discussion(self, discussion_id: str) -> None:
        found = self.session.get(Discussion, discussion_id)
        if found is None or found.tenant_id != self.tenant_id:
            raise StoreError(f"discussion {discussion_id} does not exist")
        self.session.delete(found)
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
