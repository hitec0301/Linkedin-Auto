"""Discuss: explore a source, argue with it, before it becomes a post.

A discussion is a scratchpad, not a pipeline row - it can be abandoned with
nothing left behind. Every route that touches the model goes through the
same on-demand runner and lock every other LLM-backed route uses, so a
discussion's cost is metered like any other model call and cannot race
another on-demand call for the same tenant's usage cap.
"""

from __future__ import annotations

from contextlib import closing
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from .. import discuss as discuss_mod, runner
from ..config import Config
from ..db.schema import Tenant
from ..db.store import PipelineStore, StoreError
from .deps import ON_DEMAND_LLM_LOCK, active_tenant, config, current_store
from .schemas import DiscussionMessageIn, DiscussionOut, NewDiscussionIn, RowOut

router = APIRouter(prefix="/api/discussions", tags=["discuss"])


def _out(d) -> DiscussionOut:
    return DiscussionOut(
        id=d.id, created_at=d.created_at, updated_at=d.updated_at,
        source_url=d.source_url, source_title=d.source_title,
        started_from_row_id=d.started_from_row_id, row_id=d.row_id,
        messages=d.messages,
    )


def find(store: PipelineStore, discussion_id: str):
    try:
        return store.discussion(discussion_id)
    except StoreError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.get("", response_model=List[DiscussionOut])
def list_discussions(store: PipelineStore = Depends(current_store)) -> List[DiscussionOut]:
    return [_out(d) for d in store.discussions()]


@router.get("/{discussion_id}", response_model=DiscussionOut)
def get_discussion(
    discussion_id: str, store: PipelineStore = Depends(current_store)
) -> DiscussionOut:
    return _out(find(store, discussion_id))


@router.post("", response_model=DiscussionOut, status_code=201)
def create_discussion(
    body: NewDiscussionIn,
    tenant: Tenant = Depends(active_tenant),
    cfg: Config = Depends(config),
) -> DiscussionOut:
    """Fetch the source and ask the model for a neutral summary to react to."""
    with ON_DEMAND_LLM_LOCK, closing(runner.runs("discuss", cfg, tenant_id=tenant.id)) as runs:
        run = next(runs, None)
        if run is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this account is not active")
        if body.row_id and not any(r.ID == body.row_id for r in run.store.pipeline_rows()):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such row")
        discussion = discuss_mod.start_discussion(
            run, source_url=body.source_url, source_title=body.source_title,
            started_from_row_id=body.row_id,
        )
        return _out(discussion)


@router.post("/{discussion_id}/messages", response_model=DiscussionOut)
def post_message(
    discussion_id: str,
    body: DiscussionMessageIn,
    tenant: Tenant = Depends(active_tenant),
    cfg: Config = Depends(config),
) -> DiscussionOut:
    with ON_DEMAND_LLM_LOCK, closing(runner.runs("discuss", cfg, tenant_id=tenant.id)) as runs:
        run = next(runs, None)
        if run is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this account is not active")
        discussion = find(run.store, discussion_id)
        if discussion.row_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "this discussion already became a post; start a new one to keep arguing",
            )
        updated = discuss_mod.continue_discussion(run, discussion, body.content)
        return _out(updated)


@router.post("/{discussion_id}/turn-into-post", response_model=RowOut)
def turn_into_post(
    discussion_id: str,
    tenant: Tenant = Depends(active_tenant),
    cfg: Config = Depends(config),
) -> RowOut:
    with ON_DEMAND_LLM_LOCK, closing(runner.runs("discuss", cfg, tenant_id=tenant.id)) as runs:
        run = next(runs, None)
        if run is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this account is not active")
        discussion = find(run.store, discussion_id)
        if discussion.row_id:
            raise HTTPException(status.HTTP_409_CONFLICT, "already turned into a post")
        if not any(m["role"] == "user" for m in discussion.messages):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "say something first - there is no argument yet to turn into a post",
            )
        row = discuss_mod.turn_into_post(run, discussion)
        updated = next((r for r in run.store.pipeline_rows() if r.ID == row.ID), None)
        if updated is None:  # pragma: no cover - the row cannot vanish mid-request
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such row")
        return RowOut.of(updated)


@router.delete("/{discussion_id}", status_code=204)
def delete_discussion(
    discussion_id: str,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(active_tenant),
) -> None:
    find(store, discussion_id)
    store.delete_discussion(discussion_id)
