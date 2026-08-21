"""What a job runs against: one tenant's store, sources, card and allowance.

A job serves every subscriber in turn, and the N-th customer must not lose
their week because the third one had a bad feed. So a run is a value - store,
sources, voice card, token backend, meter, and a label for the logs - and the
loop that produces them lives here, in one place, rather than in four `main()`
functions.
"""

from __future__ import annotations

import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import select

from . import llm, log
from .alerts import alert
from .config import Config
from .db.schema import Source, Tenant
from .db.session import session_factory
from .db.store import PipelineStore
from .db.usage import TenantMeter

logger = log.get(__name__)

# Only these run. A cancelled account keeps its drafts and stops being served.
RUNNABLE = ["trialing", "active"]

# The product's judgement, not the customer's: how much a source of a given
# standing should count when candidates are ranked.
TIER_WEIGHTS = {1: 0.7, 2: 1.0, 3: 1.3}


@dataclass
class Run:
    """One tenant's turn at a job.

    `label` is what appears in logs and alerts: the account's email. Never a
    token, and never a LinkedIn id, because these lines end up in an operator's
    Slack.
    """

    config: Config
    store: PipelineStore
    tenant_id: str
    label: str
    session: Any
    _sources: Optional[List[Dict[str, Any]]] = field(default=None, repr=False)

    def sources(self) -> List[Dict[str, Any]]:
        """The feeds this tenant curates from."""
        if self._sources is None:
            rows = self.session.scalars(
                select(Source)
                .where(Source.tenant_id == self.tenant_id)
                .where(Source.active.is_(True))
                .order_by(Source.tier, Source.name)
            ).all()
            self._sources = [
                {
                    "name": s.name,
                    "url": s.url,
                    "tier": s.tier,
                    "weight": TIER_WEIGHTS.get(s.tier, 1.0),
                    "audience": s.audience,
                    "enabled": True,
                    "verify": False,
                    "ingest": "rss",
                }
                for s in rows
            ]
        return self._sources

    def voice_card(self) -> str:
        return self.store.load_voice_card()

    def token_backend(self):
        """Where this run's LinkedIn tokens live."""
        from .db.tokens import DbTokenBackend

        return DbTokenBackend(self.session, self.tenant_id)

    def linkedin_app(self):
        """The tenant's own LinkedIn app, which token exchange runs against."""
        from .db.tokens import app_credentials

        return app_credentials(self.session, self.tenant_id)

    def alert(self, title: str, message: str = "", *, severity: str = "error", job: str = "") -> None:
        """Alert, tagged with which account it concerns."""
        alert(self.config, f"[{self.label}] {title}", message, severity=severity, job=job)

    def close(self) -> None:
        if self.session is not None:
            self.session.close()


def runs(job: str, config: Config, tenant_id: Optional[str] = None) -> Iterator[Run]:
    """Every account this job should serve, one at a time.

    The usage meter is installed for the duration of each run and removed
    afterwards, in a `finally`, so a crash cannot leave one tenant's meter
    attached to the next one's calls.
    """
    session = session_factory()()
    try:
        stmt = select(Tenant).where(Tenant.status.in_(RUNNABLE))
        if tenant_id:
            stmt = stmt.where(Tenant.id == tenant_id)
        tenants = session.scalars(stmt.order_by(Tenant.id)).all()
    finally:
        session.close()

    logger.info("run starting", extra={"job": job, "tenants": len(tenants)})
    for tenant in tenants:
        tenant_session = session_factory()()
        run = Run(
            config=config,
            store=PipelineStore(tenant_session, tenant.id, config),
            tenant_id=tenant.id,
            label=tenant.email or tenant.id,
            session=tenant_session,
        )
        llm.set_meter(TenantMeter(tenant_session, tenant.id, job, alerter=run.alert))
        try:
            yield run
        finally:
            llm.set_meter(None)
            run.close()


@contextmanager
def isolated(run: Run, job: str):
    """Contain one account's failure so the rest of the run continues.

    One customer's broken feed must not cost every other customer their week,
    so the failure is alerted and the loop moves on.
    """
    try:
        yield
    except llm.UsageCapExceeded as exc:
        # Not a failure. The tenant asked for more than they bought.
        logger.warning("usage cap reached", extra={"tenant": run.label, "job": job})
        run.alert(str(exc), severity="warn", job=job)
    except Exception as exc:  # noqa: BLE001 - reported, then contained
        logger.error(
            "tenant run failed",
            extra={"tenant": run.label, "job": job, "error": str(exc)},
        )
        run.alert(
            f"{job} failed: {type(exc).__name__}: {exc}",
            traceback.format_exc(),
            severity="error",
            job=job,
        )
