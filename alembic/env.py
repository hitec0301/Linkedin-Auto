"""Alembic environment.

The URL comes from DATABASE_URL rather than alembic.ini, so there is one place
a connection string lives and it is never the repository.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnp.db.schema import Base  # noqa: E402
from lnp.db.session import database_url  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", database_url())
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Write the application's custom types as what they are in the database.

    `Encrypted` is a Text column and `UtcDateTime` is a timestamptz; the extra
    behaviour is Python-side. Rendering them as themselves would make every
    migration import application code, and a migration that breaks when a class
    is renamed is a migration that cannot be replayed on an old database.
    """
    if type_ != "type":
        return False
    name = obj.__class__.__name__
    if name == "Encrypted":
        return "sa.Text()"
    if name == "UtcDateTime":
        return "sa.DateTime(timezone=True)"
    return False


def configure_opts(**extra):
    return dict(target_metadata=target_metadata, render_item=render_item, **extra)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **configure_opts(),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, **configure_opts())
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
