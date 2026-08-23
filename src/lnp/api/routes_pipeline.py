"""The pipeline: what the human sees, and the four decisions they make.

Ticking a row, writing an angle, editing the text, approving. Everything else
in this product exists to make those four things take ten minutes a week.

Approving marks a row APPROVED; the scheduled publish job takes it from there
by default. The one exception is approve's own publish_now, for a customer
who wants a post out immediately rather than waiting for a slot - it goes
through lnp.publish_now, the same POSTING/POSTED sequence and the same
LinkedIn client the scheduled job uses, so there remains exactly one place
that sequence is implemented even though there are now two callers of it.
"""

from __future__ import annotations

import threading
from contextlib import closing
from datetime import timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from .. import runner
from ..config import Config
from ..curate import curate as run_curate
from ..draft import draft_all as run_draft_all
from ..publish_now import publish_one as run_publish_one
from ..db.schema import Tenant
from ..db.store import PipelineStore
from ..models import (
    ColumnPermissionError,
    LEGAL_TRANSITIONS,
    Row,
    Status,
    TransitionError,
)
from ..db.store import StoreError
from ..util import iso, parse_dt, utcnow
from .deps import active_tenant, config, current_store
from .schemas import ApproveIn, ROW_COLUMN_BY_FIELD, ReviseIn, RowEdit, RowOut, ScheduleIn

router = APIRouter(prefix="/api/rows", tags=["pipeline"])

# A brand-new account with an empty Sources list, or one whose last run wrote
# nothing but duplicates, would otherwise let a stuck "Fetch now" button be
# clicked repeatedly - each click is a real feed fetch and a real model call.
CURATE_NOW_COOLDOWN_MINUTES = 10

# lnp.llm's usage meter is a process-global, not a per-thread one, because the
# scheduled jobs only ever run one tenant at a time in a single thread. This
# on-demand path is reached from FastAPI's threadpool, so two accounts
# clicking "Fetch now" (or two rows being redrafted) at the same instant could
# otherwise attribute one tenant's model spend to the other's cap. The lock
# serialises the rare, human-triggered case rather than making the meter
# thread-safe everywhere.
_ON_DEMAND_LLM_LOCK = threading.Lock()

# store.transition()'s guard checks the in-memory row it is handed, not a
# fresh read under a database lock, because every existing writer (the cron
# jobs) is already single-threaded and sequential. approve's publish_now is
# the first writer that is not, so two "post now" clicks landing on the same
# row from FastAPI's threadpool at once is a real - if rare - way to attempt
# the same post twice. This lock serialises this one path; it does not (and
# cannot, from here) protect against an overlap with the scheduled publish
# job running as a separate process, which is the same single-writer
# assumption the rest of the product already runs on.
_PUBLISH_NOW_LOCK = threading.Lock()

# What the interface may offer, per status. Derived from the state machine so
# a button cannot exist for a move the store would refuse.
ACTIONS: Dict[str, List[str]] = {
    Status.NEW: ["edit", "skip"],
    Status.DRAFTED: ["edit", "approve", "revise", "skip"],
    Status.REVISE: ["edit", "skip", "redraft"],
    Status.APPROVED: ["edit", "unapprove", "skip", "publish_now", "reschedule"],
    Status.POSTING: [],
    Status.POSTED: ["edit"],  # only Reach, which the human fills in later
    Status.FAILED: ["approve", "skip"],
    Status.SKIPPED: [],
    Status.EXPIRED: [],
}


def allowed_actions(row: Row) -> List[str]:
    return list(ACTIONS.get(row.status, []))


def find(store: PipelineStore, row_id: str) -> Row:
    for row in store.pipeline_rows():
        if row.ID == row_id:
            return row
    raise HTTPException(status.HTTP_404_NOT_FOUND, "no such row")


@router.get("", response_model=List[RowOut])
def list_rows(
    status_filter: Optional[str] = None,
    store: PipelineStore = Depends(current_store),
) -> List[RowOut]:
    rows = store.pipeline_rows()
    if status_filter:
        wanted = {s.strip().upper() for s in status_filter.split(",") if s.strip()}
        rows = [r for r in rows if r.status in wanted]
    return [RowOut.of(r, allowed_actions(r)) for r in rows]


@router.post("/curate-now")
def curate_now(
    tenant: Tenant = Depends(active_tenant),
    store: PipelineStore = Depends(current_store),
    cfg: Config = Depends(config),
) -> dict:
    """Fetch and score a batch right now, instead of waiting for Monday.

    Available any time, not only when Review is empty - dedupe reads the same
    URL/title history whether it is the cron job or this button asking, so a
    second fetch on top of an existing slate adds only what is genuinely new.
    Runs the exact function Job A runs on its own schedule, against this one
    account, so there is no second code path to keep in sync.
    """
    last_run = parse_dt(store.config_values().get("LAST_CURATE"))
    if last_run is not None:
        minutes_ago = (utcnow() - last_run).total_seconds() / 60
        if minutes_ago < CURATE_NOW_COOLDOWN_MINUTES:
            wait = round(CURATE_NOW_COOLDOWN_MINUTES - minutes_ago)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"a batch just ran; try again in about {wait} minute(s)",
            )

    with _ON_DEMAND_LLM_LOCK, closing(runner.runs("curate", cfg, tenant_id=tenant.id)) as runs:
        run = next(runs, None)
        if run is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this account is not active")
        try:
            written = run_curate(run)
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if written == 0:
        return {"written": 0, "detail": "checked your feeds; nothing new to show yet"}
    return {"written": written, "detail": f"{written} candidate(s) ready on Review"}


@router.post("/{row_id}/redraft-now", response_model=RowOut)
def redraft_now(
    row_id: str,
    tenant: Tenant = Depends(active_tenant),
    store: PipelineStore = Depends(current_store),
    cfg: Config = Depends(config),
) -> dict:
    """Regenerate a sent-back row right now, instead of waiting for the hourly job.

    Only valid on a REVISE row - the state the store already refuses to let
    the interface skip past. Runs the exact function Job B runs on its own
    schedule, restricted to this one row, so a manual redraft and the
    scheduled one are provably the same code path.
    """
    row = find(store, row_id)
    if row.status != Status.REVISE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this row is {row.status}, not sent back for revision",
        )

    with _ON_DEMAND_LLM_LOCK, closing(runner.runs("draft", cfg, tenant_id=tenant.id)) as runs:
        run = next(runs, None)
        if run is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this account is not active")
        try:
            run_draft_all(run, row=row_id)
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

        # Read back through run.store, the session the write just went
        # through - `store` above is a separate session/identity map and is
        # not guaranteed to see a commit made on this one.
        updated = next((r for r in run.store.pipeline_rows() if r.ID == row_id), None)
        if updated is None:  # pragma: no cover - the row cannot vanish mid-request
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such row")
        return RowOut.of(updated, allowed_actions(updated))


@router.get("/{row_id}", response_model=RowOut)
def get_row(row_id: str, store: PipelineStore = Depends(current_store)) -> RowOut:
    row = find(store, row_id)
    return RowOut.of(row, allowed_actions(row))


@router.patch("/{row_id}", response_model=RowOut)
def edit_row(
    row_id: str,
    body: RowEdit,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    """Edit the columns a person owns.

    Not DraftText. The store refuses it, and the refusal explains why: the
    difference between the draft and what was published is the only thing this
    system learns from, and folding an edit back into the draft erases it.
    """
    updates = {
        ROW_COLUMN_BY_FIELD[field]: value
        for field, value in body.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if "Selected" in updates:
        updates["Selected"] = "TRUE" if updates["Selected"] else "FALSE"
    if "Reach" in updates:
        updates["Reach"] = str(updates["Reach"])

    row = find(store, row_id)
    try:
        store.write_as_human(row, updates)
    except ColumnPermissionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except StoreError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return RowOut.of(row, allowed_actions(row))


def _validate_schedule(raw: str) -> str:
    """Parse and validate a customer-supplied publish time. Returns the ISO string to store."""
    when = parse_dt(raw)
    if when is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "could not understand that date/time"
        )
    if when < utcnow() - timedelta(minutes=1):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "that time has already passed")
    return iso(when)


def _publish_now(tenant_id: str, cfg: Config, row_id: str) -> Optional[Row]:
    """Run lnp.publish_now.publish_one for exactly one row.

    Returns the row as last observed through the session the attempt went
    through, or None if the account or the row is gone by the time the run
    starts - both of which `active_tenant` and `find` above already make
    unreachable in the ordinary case.
    """
    with _PUBLISH_NOW_LOCK, closing(runner.runs("publish", cfg, tenant_id=tenant_id)) as runs:
        run = next(runs, None)
        if run is None:
            return None
        live_row = next((r for r in run.store.pipeline_rows() if r.ID == row_id), None)
        if live_row is None:
            return None
        run_publish_one(run, live_row, dry_run=cfg.dry_run)
        return next((r for r in run.store.pipeline_rows() if r.ID == row_id), None)


def _move(store: PipelineStore, row: Row, target: str, updates=None) -> RowOut:
    try:
        store.transition(row, target, updates, allow_revision_note=True)
    except TransitionError as exc:
        # The interface offered a move the machine does not have. Say what the
        # row actually is rather than a generic 400: it usually means the row
        # changed under them, and the next thing they need is to reload.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this row is {row.status} and cannot move to {target}. "
            f"Reload to see its current state.",
        ) from exc
    return RowOut.of(row, allowed_actions(row))


@router.post("/{row_id}/approve", response_model=RowOut)
def approve(
    row_id: str,
    body: ApproveIn = ApproveIn(),
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
    cfg: Config = Depends(config),
) -> RowOut:
    """Approve a draft for publishing, and say when.

    Leaving both fields unset keeps the slot Job B already assigned - today's
    default, and still what the scheduled publish job honours either way.
    Setting one overrides that slot as part of the same decision, rather than
    a second endpoint the customer has to remember to call: approve and when
    are one moment for the person making it, not two.
    """
    if body.scheduled_for and body.publish_now:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "choose either a schedule or 'post now', not both",
        )

    row = find(store, row_id)
    if not row.effective_text.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "there is nothing to publish in this row yet",
        )

    updates = {"Error": ""}
    if body.scheduled_for:
        updates["ScheduledFor"] = _validate_schedule(body.scheduled_for)
    elif body.publish_now:
        # Due immediately, so if the publish attempt below cannot go
        # through right now, the next scheduled run (within 30 minutes)
        # picks it up rather than it waiting for whatever slot Job B guessed.
        updates["ScheduledFor"] = iso(utcnow())

    approved = _move(store, row, Status.APPROVED, updates)
    if not body.publish_now:
        return approved

    updated = _publish_now(tenant.id, cfg, row_id)
    return RowOut.of(updated, allowed_actions(updated)) if updated else approved


@router.put("/{row_id}/schedule", response_model=RowOut)
def reschedule(
    row_id: str,
    body: ScheduleIn,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    """Move an already-approved row's publish time, without unapproving it.

    APPROVED only: a row still being drafted has no commitment to move, and
    unapprove-then-reapprove is the deliberately narrow path for anything
    already posted or skipped.
    """
    row = find(store, row_id)
    if row.status != Status.APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this row is {row.status}, not approved and waiting to publish",
        )
    store.write(row, {"ScheduledFor": _validate_schedule(body.scheduled_for)})
    return RowOut.of(row, allowed_actions(row))


@router.post("/{row_id}/publish-now", response_model=RowOut)
def publish_now_route(
    row_id: str,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
    cfg: Config = Depends(config),
) -> RowOut:
    """Publish an already-approved row immediately, instead of waiting for its slot.

    The same lnp.publish_now.publish_one approve's own publish_now uses, so a
    post made from here and one made from that first, one-time choice behave
    identically - PAUSED and dry_run both still apply, and a refusal leaves
    the row APPROVED and due now for the next scheduled run.
    """
    row = find(store, row_id)
    if row.status != Status.APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this row is {row.status}, not approved and waiting to publish",
        )
    updated = _publish_now(tenant.id, cfg, row_id)
    if updated is None:  # pragma: no cover - the row cannot vanish mid-request
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such row")
    return RowOut.of(updated, allowed_actions(updated))


@router.post("/{row_id}/unapprove", response_model=RowOut)
def unapprove(
    row_id: str,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    """Take an approval back.

    APPROVED -> DRAFTED is not a legal edge - approval is meant to be a
    considered act, not a toggle - so changing your mind retires the row and
    keeps the draft readable. Deliberately slightly inconvenient.
    """
    row = find(store, row_id)
    if row.status != Status.APPROVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "this row is not approved")
    return _move(store, row, Status.SKIPPED, {"Error": "unapproved before publishing"})


@router.post("/{row_id}/revise", response_model=RowOut)
def revise(
    row_id: str,
    body: ReviseIn,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    """Send a draft back with an instruction.

    The instruction is the valuable part: a note like "stop opening with a
    question" is a rule already half-written, which is why the weekly voice
    job weights notes above inferred edits.
    """
    row = find(store, row_id)
    store.write_as_human(row, {"RevisionNote": body.note.strip()})
    return _move(store, row, Status.REVISE)


@router.post("/{row_id}/skip", response_model=RowOut)
def skip(
    row_id: str,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    row = find(store, row_id)
    if Status.SKIPPED not in LEGAL_TRANSITIONS.get(row.status, set()):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a {row.status} row cannot be skipped"
        )
    return _move(store, row, Status.SKIPPED)
