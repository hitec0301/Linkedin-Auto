"""The storage port: what the jobs need, independent of where it lives.

The pipeline started with one place to put things — a Google Sheet, which was
also the user interface. Selling this to other people breaks that: a customer
does not want to own a spreadsheet, and the operator cannot ask every customer
to run Google's OAuth consent flow. So storage becomes an interface with
adapters behind it, and the jobs stop knowing which one they are talking to.

Two invariants move *into* this module rather than living in the adapter:

  * a job never writes a human-owned column, and
  * a Status cell only ever moves along a legal edge.

They are enforced in `PipelineStore.write` and `PipelineStore.transition`,
which are concrete and call the abstract `_write_fields`. That means a new
backend cannot accidentally ship without them: to store anything at all, an
adapter has to go through the guards.

Everything here is scoped to one tenant. Multi-tenancy is not a filter the
caller remembers to apply; it is a property of the store object it was handed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import log
from .models import Row, Status, assert_transition, assert_writable
from .util import parse_bool

logger = log.get(__name__)

# Config keys the pipeline reads back out of storage. PAUSED is the kill switch.
KEY_PAUSED = "PAUSED"
KEY_POST_COUNT = "POST_COUNT"
KEY_PERSON_URN = "PERSON_URN"

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
    # Opaque handle back to the row this came from: a sheet row number, a
    # database id. Callers pass it back, they never parse it.
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


class PipelineStore(ABC):
    """Everything the four jobs do to persistent state.

    Adapters implement the abstract half. The concrete half is written once
    here, in terms of the abstract half, because it carries the guarantees.
    """

    # ---- rows: the abstract half ----------------------------------------

    @abstractmethod
    def pipeline_rows(self) -> List[Row]:
        """Every live row, oldest first. Archived rows are not included."""

    @abstractmethod
    def append_rows(self, rows: Iterable[Row]) -> int:
        """Add new rows. Returns how many were written."""

    @abstractmethod
    def _write_fields(
        self, row: Row, updates: Dict[str, Any], *, allow_revision_note: bool
    ) -> None:
        """Persist named columns of one row. Called only through `write`."""

    @abstractmethod
    def recent_index(self, days: int) -> Tuple[List[str], List[str]]:
        """(urls, titles) seen in the trailing window, live rows and archive."""

    @abstractmethod
    def archive_old_rows(self, days: int) -> int:
        """Retire finished rows older than `days`. Never deletes outright."""

    # ---- rows: the guaranteed half --------------------------------------

    def write(
        self, row: Row, updates: Dict[str, Any], *, allow_revision_note: bool = False
    ) -> None:
        """Write named columns of one row.

        Raises before touching storage if any column belongs to the human, so a
        rejected write leaves nothing half-applied.
        """
        if not updates:
            return
        assert_writable(list(updates), allow_revision_note=allow_revision_note)
        self._write_fields(row, updates, allow_revision_note=allow_revision_note)
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
        leaves storage untouched.
        """
        assert_transition(row.status, target)
        payload: Dict[str, Any] = dict(updates or {})
        payload["Status"] = target
        self.write(row, payload, allow_revision_note=allow_revision_note)
        logger.info("status changed", extra={"row_id": row.ID, "to": target})

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

    # ---- settings -------------------------------------------------------

    @abstractmethod
    def config_values(self) -> Dict[str, str]:
        """The tenant's key/value settings, keys upper-cased."""

    @abstractmethod
    def set_config_value(self, key: str, value: str) -> None:
        ...

    def is_paused(self) -> bool:
        """The kill switch.

        A settings store that cannot be read counts as paused, and so does a
        missing key. If the human's stop button is unreachable, the only safe
        reading is that it might be pressed.
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

    def bump_post_count(self) -> int:
        count = self.post_count() + 1
        self.set_config_value(KEY_POST_COUNT, str(count))
        return count

    # ---- feedback and amendments ----------------------------------------

    @abstractmethod
    def feedback_records(self) -> List[FeedbackRecord]:
        ...

    @abstractmethod
    def append_feedback(self, records: Sequence[FeedbackRecord]) -> int:
        ...

    @abstractmethod
    def amendment_records(self) -> List[AmendmentRecord]:
        ...

    @abstractmethod
    def append_amendments(self, records: Sequence[AmendmentRecord]) -> int:
        ...

    @abstractmethod
    def mark_amendment_applied(self, record: AmendmentRecord, value: str) -> None:
        ...

    def pending_amendments(self) -> List[AmendmentRecord]:
        return [r for r in self.amendment_records() if r.is_pending]

    # ---- the voice card --------------------------------------------------

    @abstractmethod
    def load_voice_card(self) -> str:
        ...

    @abstractmethod
    def save_voice_card(self, text: str) -> None:
        ...


# --------------------------------------------------------------------------
# Sheets adapter
# --------------------------------------------------------------------------


class SheetsStore(PipelineStore):
    """The original storage, behind the port.

    Kept working rather than deleted: it is what the single-tenant install
    uses, and it is the second implementation that proves the interface is an
    interface rather than a rename of one backend's method list.
    """

    def __init__(self, sheets, config):
        self.sheets = sheets
        self.config = config

    @classmethod
    def open(cls, config, sheet_id: Optional[str] = None) -> "SheetsStore":
        from .sheets import Sheets

        return cls(Sheets.open(config, sheet_id), config)

    # rows
    def pipeline_rows(self) -> List[Row]:
        return self.sheets.pipeline_rows()

    def append_rows(self, rows: Iterable[Row]) -> int:
        return self.sheets.append_rows(rows)

    def _write_fields(self, row, updates, *, allow_revision_note: bool) -> None:
        self.sheets.write(row, updates, allow_revision_note=allow_revision_note)

    def recent_index(self, days: int) -> Tuple[List[str], List[str]]:
        return self.sheets.recent_index(days)

    def archive_old_rows(self, days: int) -> int:
        return self.sheets.archive_old_rows(days)

    # settings
    def config_values(self) -> Dict[str, str]:
        return self.sheets.config_values()

    def set_config_value(self, key: str, value: str) -> None:
        self.sheets.set_config_value(key, value)

    # feedback
    def feedback_records(self) -> List[FeedbackRecord]:
        from .sheets import FEEDBACK_COLUMNS

        out = []
        for raw in self.sheets.read_tab(self.sheets.feedback_tab, FEEDBACK_COLUMNS):
            out.append(
                FeedbackRecord(
                    id=raw.get("ID", ""),
                    created_at=raw.get("CreatedAt", ""),
                    row_id=raw.get("RowID", ""),
                    signal=raw.get("Signal", ""),
                    instruction=raw.get("Instruction", ""),
                    draft_text=raw.get("DraftText", ""),
                    final_text=raw.get("FinalText", ""),
                    processed=raw.get("Processed", ""),
                    ref=raw.get("_row_number", ""),
                )
            )
        return out

    def append_feedback(self, records: Sequence[FeedbackRecord]) -> int:
        if not records:
            return 0
        return self.sheets.append_generic(
            self.sheets.feedback_tab,
            [
                [
                    r.id, r.created_at, r.row_id, r.signal, r.instruction,
                    r.draft_text, r.final_text, r.processed,
                ]
                for r in records
            ],
        )

    # amendments
    def amendment_records(self) -> List[AmendmentRecord]:
        from .sheets import AMENDMENT_COLUMNS

        out = []
        for raw in self.sheets.read_tab(self.sheets.amendments_tab, AMENDMENT_COLUMNS):
            source = [s.strip() for s in (raw.get("SourceRowIDs", "") or "").split(",")]
            out.append(
                AmendmentRecord(
                    id=raw.get("ID", ""),
                    created_at=raw.get("CreatedAt", ""),
                    rule=raw.get("Rule", ""),
                    rationale=raw.get("Rationale", ""),
                    signal=raw.get("Signal", ""),
                    occurrences=int(float(raw.get("Occurrences") or 0)),
                    recurring=bool((raw.get("Recurring", "") or "").strip()),
                    accepted=parse_bool(raw.get("Accepted")),
                    applied=raw.get("Applied", ""),
                    source_row_ids=[s for s in source if s],
                    ref=raw.get("_row_number", ""),
                )
            )
        return out

    def append_amendments(self, records: Sequence[AmendmentRecord]) -> int:
        if not records:
            return 0
        return self.sheets.append_generic(
            self.sheets.amendments_tab,
            [
                [
                    r.id, r.created_at, r.rule, r.rationale, r.signal,
                    str(r.occurrences), "RECURRING" if r.recurring else "",
                    "TRUE" if r.accepted else "FALSE", r.applied,
                    ", ".join(r.source_row_ids),
                ]
                for r in records
            ],
        )

    def mark_amendment_applied(self, record: AmendmentRecord, value: str) -> None:
        from .sheets import AMENDMENT_COLUMNS

        self.sheets.update_tab_cell(
            self.sheets.amendments_tab,
            int(record.ref),
            "Applied",
            value,
            AMENDMENT_COLUMNS,
        )

    # voice card — a file on this deployment, because a human edits it by hand
    def load_voice_card(self) -> str:
        from . import voice

        return voice.load_card(self.config)

    def save_voice_card(self, text: str) -> None:
        from . import voice

        voice.card_path(self.config).write_text(text, encoding="utf-8")


def open_store(config, tenant_id: Optional[str] = None) -> PipelineStore:
    """The store for this deployment.

    A tenant id means the hosted product, where rows live in Postgres. No
    tenant id means the single-tenant install, where they live in the Sheet the
    owner already has open.
    """
    if tenant_id:
        from .db.store import PostgresStore

        return PostgresStore.for_tenant(tenant_id, config)
    return SheetsStore.open(config)
