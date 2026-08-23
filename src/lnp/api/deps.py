"""Request-scoped dependencies: the database session, and who is asking.

`current_store` is the only way a route reaches data, and it is built from the
session cookie. There is no route that takes a tenant id as a parameter, so
there is no route that can be pointed at somebody else's account.
"""

from __future__ import annotations

import threading
from typing import Iterator, Optional

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..config import Config, load_config
from ..db.schema import Tenant
from ..db.session import session_factory
from ..db.store import PipelineStore
from .security import SESSION_COOKIE, read_session

_config: Optional[Config] = None

# lnp.llm's usage meter is a process-global, not a per-thread one, because
# every existing caller (the scheduled jobs) runs one tenant at a time in a
# single thread. Every route that opens its own runner.Run to make an
# on-demand LLM call - curate-now, redraft-now, drafting a starter voice
# card from a description - shares this one lock, so two such requests
# landing on FastAPI's threadpool at once cannot attribute one tenant's
# model spend to another's cap. One lock, not one per route module: two
# separate locks would each serialise their own callers and still race
# against each other.
ON_DEMAND_LLM_LOCK = threading.Lock()


def config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def db() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_tenant(
    session: Session = Depends(db),
    lnp_session: str = Cookie(default="", alias=SESSION_COOKIE),
) -> Tenant:
    tenant_id = read_session(lnp_session)
    if not tenant_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")
    return tenant


def current_store(
    session: Session = Depends(db),
    tenant: Tenant = Depends(current_tenant),
) -> PipelineStore:
    return PipelineStore(session, tenant.id, config())


def active_tenant(tenant: Tenant = Depends(current_tenant)) -> Tenant:
    """A tenant whose subscription still entitles them to change things.

    Reading stays open to a lapsed account: their drafts are theirs, and
    locking someone out of their own writing to collect a payment is not a
    thing this product does.
    """
    if not tenant.is_runnable:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "this subscription is not active. Your drafts are still here.",
        )
    return tenant
