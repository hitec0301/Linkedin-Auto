"""The pipeline: what the human sees, and the four decisions they make.

Ticking a row, writing an angle, editing the text, approving. Everything else
in this product exists to make those four things take ten minutes a week.

There is no endpoint here that publishes. Approving marks a row APPROVED and
the publish job takes it from there, which keeps a single writer to LinkedIn
and one place where the POSTING/POSTED sequence is enforced.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from ..db.schema import Tenant
from ..db.store import PostgresStore
from ..models import (
    ColumnPermissionError,
    LEGAL_TRANSITIONS,
    Row,
    Status,
    TransitionError,
)
from ..store import StoreError
from .deps import active_tenant, current_store
from .schemas import ROW_COLUMN_BY_FIELD, ReviseIn, RowEdit, RowOut

router = APIRouter(prefix="/api/rows", tags=["pipeline"])

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


def find(store: PostgresStore, row_id: str) -> Row:
    for row in store.pipeline_rows():
        if row.ID == row_id:
            return row
    raise HTTPException(status.HTTP_404_NOT_FOUND, "no such row")


@router.get("", response_model=List[RowOut])
def list_rows(
    status_filter: Optional[str] = None,
    store: PostgresStore = Depends(current_store),
) -> List[RowOut]:
    rows = store.pipeline_rows()
    if status_filter:
        wanted = {s.strip().upper() for s in status_filter.split(",") if s.strip()}
        rows = [r for r in rows if r.status in wanted]
    return [RowOut.of(r, allowed_actions(r)) for r in rows]


@router.get("/{row_id}", response_model=RowOut)
def get_row(row_id: str, store: PostgresStore = Depends(current_store)) -> RowOut:
    row = find(store, row_id)
    return RowOut.of(row, allowed_actions(row))


@router.patch("/{row_id}", response_model=RowOut)
def edit_row(
    row_id: str,
    body: RowEdit,
    store: PostgresStore = Depends(current_store),
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


def _move(store: PostgresStore, row: Row, target: str, updates=None) -> RowOut:
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
    store: PostgresStore = Depends(current_store),
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
    store: PostgresStore = Depends(current_store),
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
    store: PostgresStore = Depends(current_store),
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
    store: PostgresStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    row = find(store, row_id)
    if Status.SKIPPED not in LEGAL_TRANSITIONS.get(row.status, set()):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a {row.status} row cannot be skipped"
        )
    return _move(store, row, Status.SKIPPED)
