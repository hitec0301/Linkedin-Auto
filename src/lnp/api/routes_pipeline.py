"""The pipeline: what the human sees, and the decisions they make.

Every row is one card, at any stage of its life, with the same handful of
actions available throughout: edit the take, redraft with AI, set the
status directly, and - once Approved - say when it goes out. There is no
per-status set of buttons; the status dropdown is the state machine's whole
surface now; the only other module-specific action is redraft, since it is
the one that touches the model rather than just a column.
"""

from __future__ import annotations

from contextlib import closing
from datetime import timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from .. import log, runner
from ..config import Config, ConfigError
from ..curate import curate as run_curate
from ..image_gen import ImageGenError, generate_image_now as run_generate_image
from ..publish_now import publish_one as run_publish_one
from ..redraft import RedraftRefused, redraft_now as run_redraft_now
from ..db.schema import Tenant
from ..db.store import PipelineStore, StoreError
from ..models import ColumnPermissionError, Row, Status
from ..util import iso, parse_dt, utcnow
from .deps import ON_DEMAND_LLM_LOCK, PUBLISH_NOW_LOCK, active_tenant, config, current_store
from .schemas import (
    BulkSkipIn, NewPostIn, RedraftIn, ROW_COLUMN_BY_FIELD, RowEdit, RowOut, ScheduleIn, StatusIn,
)

router = APIRouter(prefix="/api/rows", tags=["pipeline"])
logger = log.get("routes_pipeline")

# A brand-new account with an empty Sources list, or one whose last run wrote
# nothing but duplicates, would otherwise let a stuck "Fetch now" button be
# clicked repeatedly - each click is a real feed fetch and a real model call.
CURATE_NOW_COOLDOWN_MINUTES = 10


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
    return [RowOut.of(r) for r in rows]


@router.post("", response_model=RowOut, status_code=201)
def create_row(
    body: NewPostIn,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    """A post someone starts themselves: a source to cite, and a take to write from.

    Everything the automatic path adds on top - dedupe, a relevance score, an
    audience and theme tag - is triage for a slate nobody asked for yet. A
    row someone deliberately started skips straight past that; it is exactly
    as real a row as a fetched one from the moment it exists, and "Redraft
    with AI" treats it identically - it fetches this SourceURL itself the
    same way it would a candidate's.
    """
    row = Row(
        SourceURL=body.source_url,
        SourceTitle=body.source_title,
        Angle=body.take,
        Status=Status.NEW,
    )
    store.append_rows([row])
    return RowOut.of(row)


@router.post("/curate-now")
def curate_now(
    tenant: Tenant = Depends(active_tenant),
    store: PipelineStore = Depends(current_store),
    cfg: Config = Depends(config),
) -> dict:
    """Fetch and score a batch right now.

    Available any time - dedupe reads the same URL/title history regardless
    of who is asking, so a second fetch on top of an existing slate adds only
    what is genuinely new. Runs the exact function the weekly job used to
    run, against this one account, so there is no second code path to keep
    in sync now that nothing runs it on a schedule.
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

    with ON_DEMAND_LLM_LOCK, closing(runner.runs("curate", cfg, tenant_id=tenant.id)) as runs:
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
def redraft_now_route(
    row_id: str,
    body: RedraftIn,
    tenant: Tenant = Depends(active_tenant),
    cfg: Config = Depends(config),
) -> RowOut:
    """Draft, revise, or - for a posted/skipped/expired row - clone and draft.

    The one AI action the interface offers, reachable from any row at any
    stage. What it does depends only on whether the row already has a draft
    to revise; see lnp.redraft for the full shape of that decision.
    """
    with ON_DEMAND_LLM_LOCK, closing(runner.runs("draft", cfg, tenant_id=tenant.id)) as runs:
        run = next(runs, None)
        if run is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this account is not active")
        row = next((r for r in run.store.pipeline_rows() if r.ID == row_id), None)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such row")
        try:
            result = run_redraft_now(run, row, body.take)
        except RedraftRefused as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        updated = next(
            (r for r in run.store.pipeline_rows() if r.ID == result.row.ID), None
        )
        if updated is None:  # pragma: no cover - the row cannot vanish mid-request
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such row")
        return RowOut.of(updated)


@router.post("/{row_id}/generate-image", response_model=RowOut)
def generate_image_route(
    row_id: str,
    tenant: Tenant = Depends(active_tenant),
    cfg: Config = Depends(config),
) -> RowOut:
    """Generate an image for this row's post and attach it as a draft.

    Model-owned, like DraftText: this always overwrites whatever image the
    row already had. Never blocks Post now on its own - a row can publish
    with or without one - so a failure here is reported plainly rather than
    retried silently.
    """
    with ON_DEMAND_LLM_LOCK, closing(runner.runs("image", cfg, tenant_id=tenant.id)) as runs:
        run = next(runs, None)
        if run is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this account is not active")
        row = next((r for r in run.store.pipeline_rows() if r.ID == row_id), None)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such row")
        try:
            updated_row = run_generate_image(run, row)
        except ImageGenError as exc:
            logger.error("image generation failed", extra={"row_id": row_id, "error": str(exc)})
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        except ConfigError as exc:
            logger.error("image generation misconfigured", extra={"row_id": row_id, "error": str(exc)})
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
        updated = next(
            (r for r in run.store.pipeline_rows() if r.ID == updated_row.ID), None
        )
        if updated is None:  # pragma: no cover - the row cannot vanish mid-request
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such row")
        return RowOut.of(updated)


@router.delete("/{row_id}/image", response_model=RowOut)
def remove_image_route(
    row_id: str,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    """Drop a generated image, going back to a text-only post."""
    row = find(store, row_id)
    store.write(row, {"ImagePrompt": "", "ImageData": ""})
    return RowOut.of(row)


@router.get("/{row_id}", response_model=RowOut)
def get_row(row_id: str, store: PipelineStore = Depends(current_store)) -> RowOut:
    return RowOut.of(find(store, row_id))


@router.patch("/{row_id}", response_model=RowOut)
def edit_row(
    row_id: str,
    body: RowEdit,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    """Edit the columns a person owns.

    `take` is not a real column: before a draft exists it is the angle,
    once one does it is the revision instruction, and which one a save
    lands in depends only on whether DraftText is populated yet - never on
    Status, so it keeps working the same way after a status the dropdown
    sets by hand as it does after one a draft or a post left behind.

    Never DraftText. The store refuses it, and the refusal explains why: the
    difference between the draft and what was published is the only thing
    this system learns from, and folding an edit back into the draft erases it.
    """
    row = find(store, row_id)
    fields = body.model_dump(exclude_unset=True)
    take = fields.pop("take", None)

    updates = {
        ROW_COLUMN_BY_FIELD[field]: value
        for field, value in fields.items()
        if value is not None
    }
    if "Selected" in updates:
        updates["Selected"] = "TRUE" if updates["Selected"] else "FALSE"
    if "Reach" in updates:
        updates["Reach"] = str(updates["Reach"])
    if take is not None:
        updates["Angle" if not row.DraftText else "RevisionNote"] = take

    try:
        store.write_as_human(row, updates)
    except ColumnPermissionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except StoreError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return RowOut.of(row)


@router.put("/{row_id}/status", response_model=RowOut)
def set_status(
    row_id: str,
    body: StatusIn,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    """Set a row's status directly - every real status, chosen freely.

    Not gated by the state machine: this is that gate, replaced by a
    dropdown on request, so an irregular jump is logged rather than
    refused. POSTING is the one exception store.set_status_freely still
    enforces - it is a marker the publish path sets itself, not a status a
    person chooses.
    """
    row = find(store, row_id)
    try:
        store.set_status_freely(row, body.status)
    except StoreError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return RowOut.of(row)


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
    with PUBLISH_NOW_LOCK, closing(runner.runs("publish", cfg, tenant_id=tenant_id)) as runs:
        run = next(runs, None)
        if run is None:
            return None
        live_row = next((r for r in run.store.pipeline_rows() if r.ID == row_id), None)
        if live_row is None:
            return None
        run_publish_one(run, live_row, dry_run=cfg.dry_run)
        return next((r for r in run.store.pipeline_rows() if r.ID == row_id), None)


@router.put("/{row_id}/schedule", response_model=RowOut)
def reschedule(
    row_id: str,
    body: ScheduleIn,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> RowOut:
    """Move an already-approved row's publish time."""
    row = find(store, row_id)
    if row.status != Status.APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this row is {row.status}, not approved and waiting to publish",
        )
    store.write(row, {"ScheduledFor": _validate_schedule(body.scheduled_for)})
    return RowOut.of(row)


@router.post("/{row_id}/publish-now", response_model=RowOut)
def publish_now_route(
    row_id: str,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
    cfg: Config = Depends(config),
) -> RowOut:
    """Publish an already-approved row immediately, instead of waiting for its slot.

    PAUSED and dry_run both still apply; a refusal leaves the row APPROVED
    and due now for the background checker to pick up.
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
    return RowOut.of(updated)


@router.post("/bulk-skip")
def bulk_skip(
    body: BulkSkipIn,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> dict:
    """Remove several rows at once - the bulk-select toolbar's one action.

    Best-effort over the list: a row already gone, or mid-publish, is
    skipped over rather than failing the whole batch, the same way one
    account's broken feed does not cost every other row in a curate run.
    """
    rows = {r.ID: r for r in store.pipeline_rows()}
    removed: List[str] = []
    for row_id in body.ids:
        row = rows.get(row_id)
        if row is None or row.status == Status.POSTING:
            continue
        store.set_status_freely(row, Status.SKIPPED)
        removed.append(row_id)
    return {"removed": removed}
