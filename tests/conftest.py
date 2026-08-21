"""Shared test scaffolding.

The database tests default to SQLite in-memory so the suite needs no server.
That has one trap worth naming: an in-memory database belongs to its
connection, so two connections are two different empty databases. StaticPool
hands every session the same one, which is what makes the API tests - where
the request runs on another thread - see the rows the test just wrote.

Set TEST_DATABASE_URL to run the same tests against real Postgres.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
