"""What a brand-new account starts with.

A tenant created by signing in has nothing: no card to draft from, no feeds to
read, no settings. Left that way, their first curate run fails with an error
about an empty source list, which is a poor first impression of a product they
just paid for.

So provisioning seeds a working starting point, and the seed is chosen to be
*correctable rather than impressive*: a deliberately plain voice card that
improves as they correct it, and the shipped source list they can prune.

Two of the seeded values are load-bearing:

  * PAUSED starts TRUE. A new account cannot post by accident, and turning it
    on is a thing the person does after they have looked at a draft.
  * The voice card carries the amendment markers, which is what the weekly
    job writes accepted rules between.
"""

from __future__ import annotations

from typing import List, Optional

import ulid
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import log
from ..config import DEFAULT_SOURCES_PATH, VOICE_CARD_PATH, load_sources
from ..store import KEY_PAUSED, KEY_POST_COUNT
from .schema import Setting, Source, Tenant, VoiceCard

logger = log.get(__name__)

DEFAULT_SETTINGS = [
    (KEY_PAUSED, "TRUE", "Nothing is published while this is TRUE."),
    (KEY_POST_COUNT, "0", "Published posts to date."),
]


def starter_card() -> str:
    """The shipped voice card, as the starting point for a new account."""
    return VOICE_CARD_PATH.read_text(encoding="utf-8")


def starter_sources() -> List[dict]:
    try:
        return load_sources(DEFAULT_SOURCES_PATH)
    except Exception as exc:  # noqa: BLE001 - a missing default is not fatal
        logger.warning("no default sources to seed", extra={"error": str(exc)})
        return []


def provision_tenant(session: Session, tenant: Tenant, with_sources: bool = True) -> None:
    """Give a new account everything it needs to run. Safe to call twice."""
    existing = {
        s.key for s in session.scalars(
            select(Setting).where(Setting.tenant_id == tenant.id)
        )
    }
    for key, value, notes in DEFAULT_SETTINGS:
        if key not in existing:
            session.add(
                Setting(tenant_id=tenant.id, key=key, value=value, notes=notes)
            )

    if session.get(VoiceCard, tenant.id) is None:
        session.add(VoiceCard(tenant_id=tenant.id, content=starter_card()))

    already_has_sources = session.scalars(
        select(Source).where(Source.tenant_id == tenant.id).limit(1)
    ).first()
    if with_sources and not already_has_sources:
        for feed in starter_sources():
            if not feed.get("enabled", True) or feed.get("ingest") != "rss":
                continue
            session.add(
                Source(
                    id=ulid.new().str,
                    tenant_id=tenant.id,
                    name=feed.get("name", ""),
                    url=feed.get("url", ""),
                    tier=int(feed.get("tier", 2)),
                    audience=feed.get("audience", "") or "",
                    # A feed the shipped list marks unverified starts switched
                    # off. A new account's first run should not open with a
                    # list of feeds that returned nothing.
                    active=not feed.get("verify", False),
                )
            )

    session.commit()
    logger.info("tenant provisioned", extra={"tenant": tenant.id})
