"""Per-tenant usage metering and the cap that goes with it.

The operator pays for inference, which is a different business from the
single-tenant tool where the owner's own API key was the natural limit. Here a
tenant with an enthusiastic source list can spend somebody else's money, so
every model call is counted against a monthly allowance and refused once it is
gone.

Recorded per call, append-only, and summed on read. A running counter would be
cheaper and would drift, and a drifting counter is either a customer cut off
early or a bill nobody budgeted for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import ulid
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import log
from ..llm import Usage, UsageCapExceeded
from .schema import Tenant, UsageEvent

logger = log.get(__name__)

# USD per million tokens, by model prefix. Used to price a call for reporting;
# the cap itself is on tokens, which do not need a price list to be correct.
PRICES = {
    "claude-opus": (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
}
DEFAULT_PRICE = (3.0, 15.0)

WARN_AT = 0.8  # tell them before the pipeline stops, not after


def period_of(when: Optional[datetime] = None) -> str:
    when = when or datetime.now(timezone.utc)
    return when.strftime("%Y-%m")


def price_for(model: str) -> tuple:
    for prefix, price in PRICES.items():
        if (model or "").startswith(prefix):
            return price
    return DEFAULT_PRICE


def cost_micros(usage: Usage) -> int:
    """What a call cost, in millionths of a dollar.

    Integer micros rather than a float of dollars: money summed over thousands
    of rows should not accumulate binary rounding error.
    """
    in_price, out_price = price_for(usage.model)
    return round(
        usage.input_tokens * in_price + usage.output_tokens * out_price
    )


@dataclass
class UsageSummary:
    period: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    cap: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def fraction_used(self) -> float:
        return (self.total_tokens / self.cap) if self.cap else 0.0

    @property
    def cost_usd(self) -> float:
        return round(self.cost_micros / 1_000_000, 4)


def summary(session: Session, tenant_id: str, period: Optional[str] = None) -> UsageSummary:
    period = period or period_of()
    row = session.execute(
        select(
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
            func.coalesce(func.sum(UsageEvent.cost_micros), 0),
        )
        .where(UsageEvent.tenant_id == tenant_id)
        .where(UsageEvent.period == period)
    ).one()
    tenant = session.get(Tenant, tenant_id)
    return UsageSummary(
        period=period,
        input_tokens=int(row[0]),
        output_tokens=int(row[1]),
        cost_micros=int(row[2]),
        cap=int(tenant.monthly_token_cap) if tenant else 0,
    )


class TenantMeter:
    """The `llm.Meter` for one tenant's job run.

    `check` is called before every model call and `record` after it, so the cap
    is enforced at the granularity of a call rather than a run: a tenant who
    runs out mid-draft loses that draft, not the week.
    """

    def __init__(self, session: Session, tenant_id: str, job: str = "", alerter=None):
        self.session = session
        self.tenant_id = tenant_id
        self.job = job
        self.alerter = alerter
        self._warned = False

    def check(self) -> None:
        current = summary(self.session, self.tenant_id)
        if current.cap and current.total_tokens >= current.cap:
            raise UsageCapExceeded(
                f"this account has used {current.total_tokens:,} of its "
                f"{current.cap:,} tokens for {current.period}. The pipeline "
                f"will resume next month, or sooner on a larger plan. Nothing "
                f"already drafted or approved is lost."
            )
        if (
            self.alerter
            and not self._warned
            and current.cap
            and current.fraction_used >= WARN_AT
        ):
            self._warned = True
            self.alerter(
                f"{current.fraction_used:.0%} of this month's drafting allowance used",
                f"{current.total_tokens:,} of {current.cap:,} tokens for "
                f"{current.period}. Drafting stops when it runs out; approving "
                f"and publishing are unaffected.",
            )

    def record(self, usage: Usage) -> None:
        self.session.add(
            UsageEvent(
                id=ulid.new().str,
                tenant_id=self.tenant_id,
                period=period_of(),
                job=self.job,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_micros=cost_micros(usage),
            )
        )
        self.session.commit()
        logger.info(
            "usage recorded",
            extra={
                "tenant": self.tenant_id,
                "job": self.job,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            },
        )
