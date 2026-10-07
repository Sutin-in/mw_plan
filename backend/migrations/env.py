"""Alembic environment. Online migrations only; URL from settings (never from alembic.ini)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from ppr.config.settings import Settings
from ppr.infrastructure.db.schema import metadata

config = context.config
target_metadata = metadata


def _url() -> str:
    x_url = context.get_x_argument(as_dictionary=True).get("url")
    if x_url:
        return x_url
    configured = config.attributes.get("url")
    if isinstance(configured, str) and configured:
        return configured
    return Settings.load().require_database_url()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (SQL script) migrations are not supported")
run_migrations_online()
