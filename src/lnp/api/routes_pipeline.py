"""The pipeline: what the human sees, and the four decisions they make.

Ticking a row, writing an angle, editing the text, approving. Everything else
in this product exists to make those four things take ten minutes a week.

There is no endpoint here that publishes. Approving marks a row APPROVED and
the publish job takes it from there, which keeps a single writer to LinkedIn
and one place where the POSTING/POSTED sequence is enforced.
"""

from __future__ import annotations

import threading
from contextlib import closing
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from .. import runner
from ..config import Config
from ..curate import curate as run_curate
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
from ..util import parse_dt, utcnow
from .deps import active_tenant, config, current_store
from .schemas import ROW_COLUMN_BY_FIELD, ReviseIn, RowEdit, RowOut

router = APIRouter(prefix="/api/rows", tags=["pipeline"])

# Everything the Review screen shows before it says "nothing waiting" - the
# same set decides whether an on-demand curate run is offered, so the button
# and the emptiness message can never disagree about what "waiting" means.
WAITING_STATUSES = {
    Status.NEW, Status.DRAFTED, Status.REVISE,
    Status.APPROVED, Status.FAILED, Status.POSTING,
}

# A brand-new account with an empty Sources list, or one whose last run wrote
# nothing but duplicates, would otherwise let a stuck "Fetch now" button be
# clicked repeatedly - each click is a real feed fetch and a real model call.
CURATE_NOW_COOLDOWN_MINUTES = 10

# lnp.llm's usage meter is a process-global, not a per-thread one, because the
# scheduled jobs only ever run one tenant at a time in a single thread. This
# on-demand path is reached from FastAPI's threadpool, so two accounts
# clicking "Fetch now" at the same instant could otherwise attribute one
# tenant's model spend to the other's cap. The lock serialises the rare,
# human-triggered case rather than making the meter thread-safe everywhere.
_CURATE_NOW_LOCK = threading.Lock()

# What the interface may offer, per status. Derived from the state machine so
# a button cannot exist for a move the store would refuse.
ACTIONS: Dict[str, List[str]] = {
    Status.NEW: ["edit", "skip"],
    Status.DRAFTED: ["edit", "approve", "revise", "skip"],
    Status.REVISE: ["edit", "skip"],
    Status.APPROVED: ["edit", "unapprove", "skip"],
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

    Offered only when there is nothing on Review already - a full slate
    exists precisely so a new account is not still empty five minutes after
    connecting LinkedIn. Runs the exact function Job A runs on its own
    schedule, against this one account, so there is no second code path to
    keep in sync.
    """
    waiting = [r for r in store.pipeline_rows() if r.status in WAITING_STATUSES]
    if waiting:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"there are already {len(waiting)} row(s) waiting on Review; "
            "clear those before fetching more",
        )

    last_run = parse_dt(store.config_values().get("LAST_CURATE"))
    if last_run is not None:
        minutes_ago = (utcnow() - last_run).total_seconds() / 60
        if minutes_ago < CURATE_NOW_COOLDOWN_MINUTES:
            wait = round(CURATE_NOW_COOLDOWN_MINUTES - minutes_ago)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"a batch just ran; try again in about {wait} minute(s)",
            )

    with _CURATE_NOW_LOCK, closing(runner.runs("curate", cfg, tenant_id=tenant.id)) as runs:
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
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    """Approve a draft for publishing.

    The one irreversible-ish decision in the product, so it is its own endpoint
    with its own name. It does not publish; it says this may be published, and
    the publish job posts it when its slot arrives.
    """
    row = find(store, row_id)
    if not row.effective_text.strip():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "there is nothing to publish in this row yet",
        )
    return _move(store, row, Status.APPROVED, {"Error": ""})


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
