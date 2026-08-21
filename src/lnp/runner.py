"""What a job runs against: one tenant's store, sources, card and allowance.

The four jobs were written for one person. Hosting them for many changes one
thing: a job now runs the same work N times, and the N-th customer must not
lose their week because the third one had a bad feed. So a run is a value —
store, sources, voice card, meter — and the loop that produces them is here,
in one place, rather than in four `main()` functions.

Which mode we are in is detected, not configured. A DATABASE_URL means the
hosted product; its absence means the single-tenant install the owner already
has working. A deployment that has to be told which it is, is a deployment
that will one day be told wrong.
"""

from __future__ import annotations

import os
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from . import llm, log
from .alerts import alert
from .config import Config, load_sources
from .store import PipelineStore, SheetsStore

logger = log.get(__name__)


def hosted() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())


@dataclass
class Run:
    """One tenant's turn at a job.

    `label` is what appears in logs and alerts. It is the account's email in
    the hosted product and "local" otherwise — never a token, and never a
    LinkedIn id, because these lines end up in an operator's Slack.
    """

    config: Config
    store: PipelineStore
    tenant_id: str = ""
    label: str = "local"
    session: Any = None
    _sources: Optional[List[Dict[str, Any]]] = field(default=None, repr=False)

    @property
    def is_hosted(self) -> bool:
        return bool(self.tenant_id)

    def sources(self) -> List[Dict[str, Any]]:
        """The feeds this tenant curates from."""
        if self._sources is not None:
            return self._sources
        if not self.is_hosted:
            self._sources = load_sources()
            return self._sources

        from sqlalchemy import select

        from .db.schema import Source

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
                # Tier weights are the product's, not the tenant's: they encode
                # how much a source of that standing should count, which is a
                # judgement the customer has no way to calibrate.
                "weight": {1: 0.7, 2: 1.0, 3: 1.3}.get(s.tier, 1.0),
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
        """Where this run's LinkedIn tokens live. None means "the default"."""
        if not self.is_hosted:
            return None
        from .db.tokens import DbTokenBackend

        return DbTokenBackend(self.session, self.tenant_id)

    def linkedin_app(self):
        """The LinkedIn app to exchange tokens against, or None for the env."""
        if not self.is_hosted:
            return None
        from .db.tokens import app_credentials

        return app_credentials(self.session, self.tenant_id)

    def alert(self, title: str, message: str = "", *, severity: str = "error", job: str = "") -> None:
        """Alert, tagged with which account it concerns."""
        prefix = f"[{self.label}] " if self.is_hosted else ""
        alert(self.config, prefix + title, message, severity=severity, job=job)

    def close(self) -> None:
        if self.session is not None:
            self.session.close()


def runs(job: str, config: Config, tenant_id: Optional[str] = None) -> Iterator[Run]:
    """Every tenant this job should serve, one at a time.

    Yields exactly one run locally. In the hosted product it yields one per
    runnable account — trialing or active — with the usage meter installed for
    the duration and removed afterwards, so a crash cannot leave one tenant's
    meter attached to the next one's calls.
    """
    if not hosted():
        yield Run(config=config, store=SheetsStore.open(config))
        return

    from sqlalchemy import select

    from .db.schema import Tenant
    from .db.session import session_factory
    from .db.store import PostgresStore
    from .db.usage import TenantMeter

    session = session_factory()()
    try:
        stmt = select(Tenant).where(Tenant.status.in_(["trialing", "active"]))
        if tenant_id:
            stmt = stmt.where(Tenant.id == tenant_id)
        tenants = session.scalars(stmt.order_by(Tenant.id)).all()
    finally:
        session.close()

    logger.info("hosted run", extra={"job": job, "tenants": len(tenants)})
    for tenant in tenants:
        tenant_session = session_factory()()
        run = Run(
            config=config,
            store=PostgresStore(tenant_session, tenant.id, config),
            tenant_id=tenant.id,
            label=tenant.email or tenant.id,
            session=tenant_session,
        )
        meter = TenantMeter(tenant_session, tenant.id, job, alerter=run.alert)
        llm.set_meter(meter)
        try:
            yield run
        finally:
            llm.set_meter(None)
            run.close()


@contextmanager
def isolated(run: Run, job: str):
    """Contain one tenant's failure so the rest of the run continues.

    Locally this re-raises, because there is nobody else's work to protect and
    a non-zero exit is how the owner finds out. Hosted, it alerts and moves on:
    one customer's broken feed must not cost every other customer their week.
    """
    try:
        yield
    except llm.UsageCapExceeded as exc:
        # Not a failure. The tenant asked for more than they bought.
        logger.warning("usage cap reached", extra={"tenant": run.label, "job": job})
        run.alert(str(exc), severity="warn", job=job)
        if not run.is_hosted:
            raise
    except Exception as exc:  # noqa: BLE001 - re-raised locally, contained hosted
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
        if not run.is_hosted:
            raise
