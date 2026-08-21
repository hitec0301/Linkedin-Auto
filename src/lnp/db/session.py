"""Engine and session handling.

One engine per process, created lazily so importing the package does not
require a database — the single-tenant install has none, and the tests build
their own.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .schema import Base

ENV_URL = "DATABASE_URL"

_engine: Optional[Engine] = None
_sessions: Optional[sessionmaker] = None


def normalize_url(url: str) -> str:
    """Accept the URL shape hosts actually hand out.

    Railway, Heroku and friends emit `postgres://`, which SQLAlchemy 2 no
    longer recognises, and `postgresql://` selects psycopg2. Point both at
    psycopg 3 rather than making the operator know that.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def database_url() -> str:
    url = os.environ.get(ENV_URL, "").strip()
    if not url:
        raise RuntimeError(
            f"{ENV_URL} is not set. The hosted product needs a Postgres "
            f"database; on Railway this variable is provided by the Postgres "
            f"service and referenced from each other service."
        )
    return normalize_url(url)


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            database_url(),
            pool_pre_ping=True,  # a pooled connection outlives a host restart
            pool_size=5,
            max_overflow=10,
            future=True,
        )
    return _engine


def session_factory() -> sessionmaker:
    global _sessions
    if _sessions is None:
        _sessions = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _sessions


def configure(bind: Engine) -> None:
    """Point the module at a specific engine. Used by tests and by the API."""
    global _engine, _sessions
    _engine = bind
    _sessions = sessionmaker(bind=bind, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(bind: Optional[Engine] = None) -> None:
    """Create the schema. Alembic owns migrations; this is for tests and first
    boot of an empty database."""
    Base.metadata.create_all(bind or engine())
