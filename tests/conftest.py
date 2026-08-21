"""Shared test scaffolding.

The database tests default to SQLite in-memory so the suite needs no server.
That has one trap worth naming: an in-memory database belongs to its
connection, so two connections are two different empty databases. StaticPool
hands every session the same one, which is what makes the API tests - where the
request runs on another thread - see the rows the test just wrote.

Set TEST_DATABASE_URL to run the same tests against real Postgres.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TENANT = "01J000000000000000000000AA"
OTHER_TENANT = "01J000000000000000000000BB"


def make_engine():
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if url:
        from lnp.db.session import normalize_url

        return create_engine(normalize_url(url))
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def pipeline_config():
    from lnp.config import Config

    return Config({
        "retention": {"archive_after_days": 90},
        "publish": {"staleness_hours": 48, "dry_run": True, "max_posts_per_run": 1},
    })



@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    """Every test gets its own key, so no test can depend on another's."""
    from lnp.db import crypto

    monkeypatch.setenv(crypto.ENV_KEY, crypto.generate_key())


def new_database(tenants=(TENANT, OTHER_TENANT), provision=True):
    """A fresh schema with the given accounts, wired up as the live engine.

    Accounts are provisioned exactly as a real sign-up provisions them - the
    starter voice card and the shipped feeds - so a job test exercises what a
    customer's first run actually meets, not a hand-built fixture that cannot
    tell you when provisioning has broken.
    """
    from lnp.db import session as session_mod
    from lnp.db.provision import provision_tenant
    from lnp.db.schema import Base, Tenant

    engine = make_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session_mod.configure(engine)

    with session_mod.session_scope() as session:
        for index, tenant_id in enumerate(tenants):
            tenant = Tenant(
                id=tenant_id,
                linkedin_sub=f"sub-{index}",
                email=f"{'one' if index == 0 else 'two'}@example.test",
                status="active",
            )
            session.add(tenant)
            session.flush()
            if provision:
                provision_tenant(session, tenant)
    return engine


def make_store(rows=(), paused="FALSE", tenant_id=TENANT, tenants=None, provision=True):
    """A store for one account, seeded with rows and the two settings.

    `provision=False` gives a bare account - no card, no feeds - which is what
    a test about what happens *before* setup needs.
    """
    from sqlalchemy.orm import Session

    from lnp.db import session as session_mod
    from lnp.db.store import PipelineStore

    new_database(tenants or (TENANT, OTHER_TENANT), provision=provision)
    store = PipelineStore(Session(session_mod.engine()), tenant_id, pipeline_config())
    store.set_config_value("PAUSED", paused)
    store.set_config_value("POST_COUNT", "3")
    if rows:
        store.append_rows(list(rows))
    return store


def rows_of(store):
    """Re-read rows a *different* session wrote.

    The job under test runs on its own session, so the test's session can be
    holding instances from before that commit. Expiring first means the
    assertion reads the database rather than its own memory of it.
    """
    store.session.expire_all()
    return store.pipeline_rows()


def make_run(store, alerts=None, card="# Voice\n"):
    """A `runner.Run` over a store, with alerts captured if asked."""
    from lnp import runner as runner_mod

    run = runner_mod.Run(
        config=store.config,
        store=store,
        tenant_id=store.tenant_id,
        label="one@example.test",
        session=store.session,
    )
    if alerts is not None:
        run.alert = lambda title, body="", **kw: alerts.append(title)
    if card is not None:
        store.save_voice_card(card)
    return run


def connect_linkedin(store, access_token="test-access-token"):
    """Give an account a LinkedIn app and a posting grant.

    The publish job asks for both before it does anything, which is the point:
    an account that has not finished setup cannot publish, and the test should
    have to satisfy that rather than patch around it.
    """
    from lnp.db.tokens import DbTokenBackend, save_app_credentials
    from lnp.tokens import TokenSet
    from lnp.util import iso, utcnow
    from datetime import timedelta

    save_app_credentials(
        store.session, store.tenant_id, "tenantclientid",
        "WPL_AP1.EXAMPLE0000FAKE.aBcDeF==", "https://app.test/callback",
    )
    DbTokenBackend(store.session, store.tenant_id).save(
        TokenSet(
            access_token=access_token,
            refresh_token="test-refresh-token",
            expires_at=iso(utcnow() + timedelta(days=59)),
            refresh_expires_at=iso(utcnow() + timedelta(days=364)),
        )
    )
    return store
