"""Google Sheet access — the pipeline's only persistent state.

The Sheet is also the user interface: everything the human does, they do in a
cell. Two guarantees this module enforces on every write:

  * a job never writes a human-owned column (`assert_writable`), and
  * a Status cell only ever moves along a legal edge (`assert_transition`).

Both raise rather than warn. A pipeline that silently overwrote somebody's
Angle would be worse than one that crashed.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import gspread
from google.oauth2.service_account import Credentials

from . import log
from .config import Config, google_credentials_info, require_env
from .models import (
    COLUMNS,
    COLUMN_INDEX,
    Row,
    Status,
    assert_transition,
    assert_writable,
)
from .util import iso, parse_dt, utcnow

logger = log.get(__name__)

# Sheets only. Nothing here touches the Drive API: the sheet is opened by id,
# never searched for by name, so a Drive scope would be permission we never use.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HISTORY_COLUMNS: List[str] = COLUMNS + ["ArchivedAt"]

FEEDBACK_COLUMNS: List[str] = [
    "ID",
    "CreatedAt",
    "RowID",
    "Signal",  # NOTE (human wrote a rule) or DIFF (human edited the text)
    "Instruction",
    "DraftText",
    "FinalText",
    "Processed",
]

AMENDMENT_COLUMNS: List[str] = [
    "ID",
    "CreatedAt",
    "Rule",
    "Rationale",
    "Signal",
    "Occurrences",
    "Recurring",
    "Accepted",  # the human's checkbox — nothing enters the card without it
    "Applied",
    "SourceRowIDs",
]

CONFIG_COLUMNS: List[str] = ["Key", "Value", "Notes"]

# Seeded into the Config tab by setup_sheet.py. PAUSED is the kill switch.
CONFIG_DEFAULTS: List[List[str]] = [
    ["PAUSED", "FALSE", "TRUE stops Job C (publish) immediately. Edit from your phone."],
    ["POST_COUNT", "0", "Published posts to date. Maintained by the publish job."],
    ["PERSON_URN", "", "Cached LinkedIn member URN. Maintained by the publish job."],
    ["LAST_CURATE", "", "Timestamp of the last successful curate run."],
    ["LAST_PUBLISH", "", "Timestamp of the last successful publish run."],
]


class SheetError(Exception):
    pass


def a1_column(index_zero_based: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    n = index_zero_based + 1
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _retry(fn, *, attempts: int = 4, base_delay: float = 2.0, what: str = "sheet call"):
    """Retry transient Google API failures with exponential backoff.

    Sheets returns 429/500 under normal use. Failing a whole publish run on one
    of those would strand rows in POSTING, which is the expensive failure.
    """
    last: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return fn()
        except gspread.exceptions.APIError as exc:  # pragma: no cover - network
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status not in (429, 500, 502, 503, 504):
                raise
            last = exc
            delay = base_delay * (2**attempt)
            logger.warning(
                "sheet call failed, retrying", extra={"what": what, "delay": delay, "status": status}
            )
            time.sleep(delay)
    raise SheetError(f"{what} failed after {attempts} attempts: {last}")


class Sheets:
    """Typed access to the five tabs."""

    def __init__(self, spreadsheet, config: Config):
        self.ss = spreadsheet
        self.config = config
        self.pipeline_tab = config.get("sheet.pipeline_tab", "Pipeline")
        self.history_tab = config.get("sheet.history_tab", "History")
        self.feedback_tab = config.get("sheet.feedback_tab", "Feedback")
        self.amendments_tab = config.get("sheet.amendments_tab", "VoiceAmendments")
        self.config_tab = config.get("sheet.config_tab", "Config")

    # ---- construction ---------------------------------------------------

    @classmethod
    def open(cls, config: Config, sheet_id: Optional[str] = None) -> "Sheets":
        info = google_credentials_info()
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet_id = sheet_id or require_env("SHEET_ID")
        logger.info("opening sheet", extra={"sheet_id": sheet_id, "sa": info.get("client_email")})
        return cls(_retry(lambda: client.open_by_key(sheet_id), what="open_by_key"), config)

    def worksheet(self, title: str):
        try:
            return _retry(lambda: self.ss.worksheet(title), what=f"worksheet({title})")
        except gspread.exceptions.WorksheetNotFound as exc:
            raise SheetError(
                f"tab {title!r} not found; run `python scripts/setup_sheet.py` first"
            ) from exc

    # ---- reading --------------------------------------------------------

    def pipeline_rows(self) -> List[Row]:
        """Every Pipeline row, carrying its 1-based sheet row number."""
        ws = self.worksheet(self.pipeline_tab)
        values = _retry(ws.get_all_values, what="pipeline get_all_values")
        if not values:
            return []
        header, body = values[0], values[1:]
        _check_header(header, COLUMNS, self.pipeline_tab)
        return [
            Row.from_values(raw, row_number=i)
            for i, raw in enumerate(body, start=2)
            if any(str(cell).strip() for cell in raw)
        ]

    def history_rows(self) -> List[List[str]]:
        try:
            ws = self.worksheet(self.history_tab)
        except SheetError:
            return []
        values = _retry(ws.get_all_values, what="history get_all_values")
        return values[1:] if values else []

    def recent_index(self, days: int) -> Tuple[List[str], List[str]]:
        """URLs and titles seen in the trailing window, Pipeline and History.

        Dedupe compares against these, so a story that ran three weeks ago does
        not come back around when a second outlet picks it up. Both lists come
        from one pass: the Sheets API has a per-minute read quota, and this runs
        against every candidate in the week.
        """
        cutoff = utcnow() - timedelta(days=days)
        urls: List[str] = []
        titles: List[str] = []

        for row in self.pipeline_rows():
            created = parse_dt(row.CreatedAt)
            if created is not None and created < cutoff:
                continue
            if row.SourceURL:
                urls.append(row.SourceURL)
            if row.SourceTitle:
                titles.append(row.SourceTitle)

        url_idx = COLUMN_INDEX["SourceURL"]
        title_idx = COLUMN_INDEX["SourceTitle"]
        created_idx = COLUMN_INDEX["CreatedAt"]
        for raw in self.history_rows():
            created = parse_dt(raw[created_idx]) if len(raw) > created_idx else None
            if created is not None and created < cutoff:
                continue
            if len(raw) > url_idx and raw[url_idx]:
                urls.append(str(raw[url_idx]))
            if len(raw) > title_idx and raw[title_idx]:
                titles.append(str(raw[title_idx]))
        return urls, titles

    def published_rows(self) -> List[Row]:
        return [r for r in self.pipeline_rows() if r.status == Status.POSTED]

    # ---- writing --------------------------------------------------------

    def append_rows(self, rows: Iterable[Row]) -> int:
        payload = [r.touch_created().to_values() for r in rows]
        if not payload:
            return 0
        ws = self.worksheet(self.pipeline_tab)
        _retry(
            lambda: ws.append_rows(payload, value_input_option="RAW"),
            what="append_rows",
        )
        logger.info("appended pipeline rows", extra={"count": len(payload)})
        return len(payload)

    def write(
        self,
        row: Row,
        updates: Dict[str, Any],
        *,
        allow_revision_note: bool = False,
    ) -> None:
        """Write named columns of one row.

        Raises before touching the network if any column belongs to the human.
        """
        if not updates:
            return
        if row.row_number is None:
            raise SheetError(f"row {row.ID} has no sheet row number; cannot write")
        assert_writable(list(updates), allow_revision_note=allow_revision_note)

        ws = self.worksheet(self.pipeline_tab)
        data = []
        for name, value in updates.items():
            col = a1_column(COLUMN_INDEX[name])
            data.append(
                {
                    "range": f"{col}{row.row_number}",
                    "values": [["" if value is None else str(value)]],
                }
            )
        _retry(
            lambda: ws.batch_update(data, value_input_option="RAW"),
            what="row batch_update",
        )
        for name, value in updates.items():
            setattr(row, name, "" if value is None else str(value))
        logger.info(
            "row updated",
            extra={"row_id": row.ID, "row_number": row.row_number, "fields": list(updates)},
        )

    def transition(
        self,
        row: Row,
        target: str,
        updates: Optional[Dict[str, Any]] = None,
        *,
        allow_revision_note: bool = False,
    ) -> None:
        """Move a row to `target`, writing any other columns in the same call.

        The transition is checked before anything is written, so an illegal move
        leaves the Sheet untouched.
        """
        assert_transition(row.status, target)
        payload: Dict[str, Any] = dict(updates or {})
        payload["Status"] = target
        self.write(row, payload, allow_revision_note=allow_revision_note)
        logger.info(
            "status changed",
            extra={"row_id": row.ID, "to": target},
        )

    # ---- Config tab ------------------------------------------------------

    def config_values(self) -> Dict[str, str]:
        ws = self.worksheet(self.config_tab)
        values = _retry(ws.get_all_values, what="config get_all_values")
        out: Dict[str, str] = {}
        for raw in values[1:] if values else []:
            if raw and str(raw[0]).strip():
                out[str(raw[0]).strip().upper()] = (
                    str(raw[1]).strip() if len(raw) > 1 else ""
                )
        return out

    def is_paused(self) -> bool:
        """The kill switch. Any truthy value in PAUSED stops the publish job.

        A missing Config tab or a missing key counts as paused: if the human's
        stop button is unreachable, the safe reading is that it might be on.
        """
        try:
            values = self.config_values()
        except SheetError:
            logger.error("config tab unreadable; treating pipeline as PAUSED")
            return True
        raw = values.get("PAUSED", "")
        if raw == "":
            logger.error("PAUSED key missing from Config tab; treating as PAUSED")
            return True
        return str(raw).strip().lower() in {"true", "yes", "1", "on", "paused"}

    def set_config_value(self, key: str, value: str) -> None:
        ws = self.worksheet(self.config_tab)
        values = _retry(ws.get_all_values, what="config get_all_values")
        target_row = None
        for i, raw in enumerate(values[1:] if values else [], start=2):
            if raw and str(raw[0]).strip().upper() == key.upper():
                target_row = i
                break
        if target_row is None:
            _retry(
                lambda: ws.append_row([key, str(value), ""], value_input_option="RAW"),
                what="config append_row",
            )
        else:
            _retry(
                lambda: ws.update(
                    values=[[str(value)]], range_name=f"B{target_row}", value_input_option="RAW"
                ),
                what="config update",
            )

    def bump_post_count(self) -> int:
        current = self.config_values().get("POST_COUNT", "0")
        try:
            count = int(float(current)) + 1
        except ValueError:
            count = 1
        self.set_config_value("POST_COUNT", str(count))
        return count

    def post_count(self) -> int:
        """Published posts to date — decides variants and few-shot retrieval."""
        try:
            return int(float(self.config_values().get("POST_COUNT", "0") or 0))
        except (ValueError, SheetError):
            return 0

    # ---- Feedback and amendments ----------------------------------------

    def append_generic(self, tab: str, values: Sequence[Sequence[Any]]) -> int:
        payload = [[("" if v is None else str(v)) for v in row] for row in values]
        if not payload:
            return 0
        ws = self.worksheet(tab)
        _retry(
            lambda: ws.append_rows(payload, value_input_option="RAW"),
            what=f"{tab} append_rows",
        )
        return len(payload)

    def read_tab(self, tab: str, expected: Optional[List[str]] = None) -> List[Dict[str, str]]:
        ws = self.worksheet(tab)
        values = _retry(ws.get_all_values, what=f"{tab} get_all_values")
        if not values:
            return []
        header = [str(h).strip() for h in values[0]]
        if expected:
            _check_header(header, expected, tab)
        out = []
        for i, raw in enumerate(values[1:], start=2):
            if not any(str(cell).strip() for cell in raw):
                continue
            record = {
                name: (str(raw[j]) if j < len(raw) else "")
                for j, name in enumerate(header)
            }
            record["_row_number"] = str(i)
            out.append(record)
        return out

    def update_tab_cell(self, tab: str, row_number: int, column: str, value: Any,
                        header: Optional[List[str]] = None) -> None:
        ws = self.worksheet(tab)
        header = header or _retry(lambda: ws.row_values(1), what=f"{tab} header")
        if column not in header:
            raise SheetError(f"column {column!r} not found in tab {tab!r}")
        col = a1_column(header.index(column))
        _retry(
            lambda: ws.update(
                values=[[str(value)]], range_name=f"{col}{row_number}", value_input_option="RAW"
            ),
            what=f"{tab} update cell",
        )

    def expire_stale(
        self, rows: Sequence[Row], staleness_hours: int, now=None
    ) -> List[Row]:
        """Expire rows too far past their slot to be worth posting.

        A four-day-old take is worse than no post, so an overdue row is retired
        rather than published late. Only DRAFTED and APPROVED rows can expire:
        a row mid-flight in POSTING is a different problem (see the runbook).
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

    # ---- archiving -------------------------------------------------------

    def archive_old_rows(self, days: int) -> int:
        """Move rows older than `days` to History. Rows are never deleted here
        without first being written to History."""
        cutoff = utcnow() - timedelta(days=days)
        rows = self.pipeline_rows()
        stale = [
            r
            for r in rows
            if (parse_dt(r.CreatedAt) or utcnow()) < cutoff
            and r.status in {Status.POSTED, Status.SKIPPED, Status.EXPIRED}
        ]
        if not stale:
            return 0
        now = iso()
        self.append_generic(self.history_tab, [r.to_values() + [now] for r in stale])

        ws = self.worksheet(self.pipeline_tab)
        # Delete bottom-up so earlier deletions do not shift later row numbers.
        for row in sorted(stale, key=lambda r: r.row_number or 0, reverse=True):
            _retry(
                lambda rn=row.row_number: ws.delete_rows(rn),
                what="delete archived row",
            )
        logger.info("archived rows to history", extra={"count": len(stale)})
        return len(stale)


def _check_header(actual: Sequence[str], expected: Sequence[str], tab: str) -> None:
    actual_trimmed = [str(h).strip() for h in actual][: len(expected)]
    if actual_trimmed != list(expected):
        raise SheetError(
            f"tab {tab!r} header does not match the expected layout.\n"
            f"expected: {list(expected)}\nfound:    {actual_trimmed}\n"
            f"run `python scripts/setup_sheet.py` to repair it"
        )
