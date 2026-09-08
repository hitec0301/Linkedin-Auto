"""The account: who you are, the kill switch, sources, voice, usage, health."""

from __future__ import annotations

from typing import List

import ulid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from contextlib import closing

from .. import runner, voice
from ..config import Config
from ..db.schema import LinkedInApp, LinkedInToken, Source, VoiceCard
from ..db.schema import Tenant
from ..db.store import PipelineStore
from ..db.usage import summary as usage_summary
from ..models import health_stats
from ..db.store import KEY_AUDIENCE_DESCRIPTION, KEY_PAUSED, StoreError
from ..util import iso
from .deps import ON_DEMAND_LLM_LOCK, active_tenant, config, current_store, current_tenant, db
from .schemas import (
    AmendmentDecision,
    AmendmentOut,
    AudienceIn,
    HealthOut,
    MeOut,
    PauseIn,
    SettingsIn,
    SourceIn,
    SourceOut,
    UsageOut,
    VoiceCardIn,
    VoiceCardOut,
)

router = APIRouter(prefix="/api", tags=["account"])


@router.get("/me", response_model=MeOut)
def me(
    session: Session = Depends(db),
    tenant: Tenant = Depends(current_tenant),
    store: PipelineStore = Depends(current_store),
) -> MeOut:
    app = session.get(LinkedInApp, tenant.id)
    token = session.get(LinkedInToken, tenant.id)
    return MeOut(
        id=tenant.id,
        email=tenant.email,
        name=tenant.name,
        picture_url=tenant.picture_url,
        status=tenant.status,
        plan=tenant.plan,
        timezone=tenant.timezone,
        paused=store.is_paused(),
        onboarded=tenant.onboarded_at is not None,
        linkedin_app_configured=bool(app and app.client_id),
        linkedin_connected=bool(token and token.access_token),
        post_count=store.post_count(),
        audience_description=store.config_values().get(KEY_AUDIENCE_DESCRIPTION, ""),
    )


@router.put("/settings", response_model=MeOut)
def update_settings(
    body: SettingsIn,
    session: Session = Depends(db),
    tenant: Tenant = Depends(active_tenant),
    store: PipelineStore = Depends(current_store),
) -> MeOut:
    if body.timezone:
        tenant.timezone = body.timezone
    session.commit()
    return me(session, tenant, store)


@router.put("/pause")
def set_paused(
    body: PauseIn,
    store: PipelineStore = Depends(current_store),
    tenant: Tenant = Depends(current_tenant),
) -> dict:
    """The kill switch.

    Available to a lapsed account too, and unpausing is the only thing here a
    lapsed account can still do — because the one setting nobody should ever
    be locked out of is the one that stops posts going out.
    """
    store.set_config_value(KEY_PAUSED, "TRUE" if body.paused else "FALSE")
    return {"paused": body.paused}


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def _source_out(s: Source) -> SourceOut:
    return SourceOut(
        id=s.id, name=s.name, url=s.url, tier=s.tier, audience=s.audience,
        active=s.active, last_ok_at=iso(s.last_ok_at) if s.last_ok_at else "",
        last_error=s.last_error,
    )


@router.get("/sources", response_model=List[SourceOut])
def list_sources(
    session: Session = Depends(db), tenant: Tenant = Depends(current_tenant)
) -> List[SourceOut]:
    rows = session.scalars(
        select(Source).where(Source.tenant_id == tenant.id).order_by(Source.tier, Source.name)
    ).all()
    return [_source_out(s) for s in rows]


@router.post("/sources", response_model=SourceOut, status_code=201)
def add_source(
    body: SourceIn,
    session: Session = Depends(db),
    tenant: Tenant = Depends(active_tenant),
) -> SourceOut:
    source = Source(
        id=ulid.new().str, tenant_id=tenant.id, name=body.name.strip(),
        url=body.url.strip(), tier=body.tier, audience=body.audience,
        active=body.active,
    )
    session.add(source)
    session.commit()
    return _source_out(source)


@router.put("/sources/{source_id}", response_model=SourceOut)
def update_source(
    source_id: str,
    body: SourceIn,
    session: Session = Depends(db),
    tenant: Tenant = Depends(active_tenant),
) -> SourceOut:
    source = session.get(Source, source_id)
    if source is None or source.tenant_id != tenant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such source")
    source.name = body.name.strip()
    source.url = body.url.strip()
    source.tier = body.tier
    source.audience = body.audience
    source.active = body.active
    session.commit()
    return _source_out(source)


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(
    source_id: str,
    session: Session = Depends(db),
    tenant: Tenant = Depends(active_tenant),
) -> None:
    source = session.get(Source, source_id)
    if source is None or source.tenant_id != tenant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such source")
    session.delete(source)
    session.commit()


# --------------------------------------------------------------------------
# Audience: who this account writes for, and the voice card it seeds
# --------------------------------------------------------------------------


@router.put("/audience", response_model=VoiceCardOut)
def set_audience(
    body: AudienceIn,
    tenant: Tenant = Depends(active_tenant),
    cfg: Config = Depends(config),
) -> VoiceCardOut:
    """Save who this account writes for, and draft a starter card from it.

    Only "Who is writing" and "Stance" are regenerated - Structure, Banned,
    Formatting, the amendment markers, and anything already hand-edited stay
    exactly as they are. This can be called again later to redo those two
    sections from a fresh description; it is not a one-time setup step.

    Goes through its own runner.Run, the same as the pipeline's other
    on-demand LLM calls, so this call is metered against the account's
    monthly allowance like every other model call rather than being free.
    """
    description = body.description
    with ON_DEMAND_LLM_LOCK, closing(runner.runs("voice_amend", cfg, tenant_id=tenant.id)) as runs:
        run = next(runs, None)
        if run is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this account is not active")
        run.store.set_config_value(KEY_AUDIENCE_DESCRIPTION, description)
        sections = voice.draft_starter_sections(run.config, description)
        updated_card = voice.replace_starter_sections(run.store.load_voice_card(), sections)
        run.store.save_voice_card(updated_card)
        card = run.session.get(VoiceCard, tenant.id)
        return VoiceCardOut(content=card.content, updated_at=iso(card.updated_at))


# --------------------------------------------------------------------------
# Voice
# --------------------------------------------------------------------------


@router.get("/voice-card", response_model=VoiceCardOut)
def get_voice_card(
    session: Session = Depends(db),
    tenant: Tenant = Depends(current_tenant),
) -> VoiceCardOut:
    card = session.get(VoiceCard, tenant.id)
    if card is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no voice card yet")
    return VoiceCardOut(content=card.content, updated_at=iso(card.updated_at))


@router.put("/voice-card", response_model=VoiceCardOut)
def put_voice_card(
    body: VoiceCardIn,
    session: Session = Depends(db),
    tenant: Tenant = Depends(active_tenant),
    store: PipelineStore = Depends(current_store),
) -> VoiceCardOut:
    """Replace the voice card.

    Editable in full, by hand, at any time. That is the whole design: when a
    draft comes out wrong on a Tuesday, the fix is a sentence in a text box,
    not a retraining run.
    """
    store.save_voice_card(body.content)
    card = session.get(VoiceCard, tenant.id)
    return VoiceCardOut(content=card.content, updated_at=iso(card.updated_at))


@router.get("/amendments", response_model=List[AmendmentOut])
def list_amendments(store: PipelineStore = Depends(current_store)) -> List[AmendmentOut]:
    return [
        AmendmentOut(
            id=r.id, created_at=r.created_at, rule=r.rule, rationale=r.rationale,
            signal=r.signal, occurrences=r.occurrences, recurring=r.recurring,
            accepted=r.accepted, applied=r.applied, source_row_ids=r.source_row_ids,
        )
        for r in store.amendment_records()
    ]


@router.put("/amendments/{amendment_id}", response_model=AmendmentOut)
def decide_amendment(
    amendment_id: str,
    body: AmendmentDecision,
    session: Session = Depends(db),
    tenant: Tenant = Depends(active_tenant),
    store: PipelineStore = Depends(current_store),
) -> AmendmentOut:
    """Tick or untick a proposed rule.

    This endpoint is the only way `accepted` ever becomes true, and
    accepting writes it into the voice card immediately - there is no
    weekly job left to do that later, so this is the only moment it can
    happen. Nothing the model produces reaches the card without a person
    doing this first.
    """
    from ..db.schema import VoiceAmendment

    found = session.get(VoiceAmendment, amendment_id)
    if found is None or found.tenant_id != tenant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such amendment")
    if found.applied:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this rule is already in the card; edit the card to change it",
        )
    found.accepted = body.accepted
    session.commit()
    if body.accepted:
        voice.apply_accepted(store)
    return AmendmentOut(
        id=found.id, created_at=iso(found.created_at), rule=found.rule,
        rationale=found.rationale, signal=found.signal,
        occurrences=found.occurrences, recurring=found.recurring,
        accepted=found.accepted, applied=found.applied,
        source_row_ids=list(found.source_row_ids or []),
    )


# --------------------------------------------------------------------------
# Usage and health
# --------------------------------------------------------------------------


@router.get("/usage", response_model=UsageOut)
def usage(
    session: Session = Depends(db), tenant: Tenant = Depends(current_tenant)
) -> UsageOut:
    current = usage_summary(session, tenant.id)
    return UsageOut(
        period=current.period,
        input_tokens=current.input_tokens,
        output_tokens=current.output_tokens,
        total_tokens=current.total_tokens,
        cap=current.cap,
        fraction_used=round(current.fraction_used, 4),
        cost_usd=current.cost_usd,
    )


@router.get("/health-metric", response_model=HealthOut)
def health(store: PipelineStore = Depends(current_store)) -> HealthOut:
    """The measurement that decides whether this is worth paying for.

    Reported to the customer, not just to the operator, verdict included. A
    tool that costs more editing time than it saves should say so to the
    person paying for it.
    """
    cfg = config()
    stats = health_stats(
        store.pipeline_rows(),
        window=int(cfg.get("health.window", 10)),
        floor=float(cfg.get("health.clean_publish_floor", 0.5)),
        evaluate_after_days=int(cfg.get("health.evaluate_after_days", 30)),
    )
    return HealthOut(
        published=stats.published, clean=stats.clean, clean_rate=stats.clean_rate,
        mean_edit_distance=stats.mean_edit_distance,
        mean_edit_distance_first=stats.mean_edit_distance_first,
        mean_edit_distance_last=stats.mean_edit_distance_last,
        days_running=stats.days_running, verdict=stats.verdict,
        improving=stats.improving,
    )
