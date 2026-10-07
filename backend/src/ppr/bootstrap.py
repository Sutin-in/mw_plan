"""Composition helpers: choose the HOSxP implementation from settings.

Outside the core on purpose: only this module (and the CLI / app start-up that call it)
may know the concrete mock or real adapter (import-linter keeps the core independent).
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine

from ppr.config.settings import Settings, SettingsError
from ppr.integration.hosxp.gateway import HosxpGateway
from ppr.integration.hosxp.mock import MockHosxpGateway
from ppr.integration.hosxp.mock.fixtures import sample_data
from ppr.integration.hosxp.real.query_config import load_config
from ppr.integration.hosxp.real.sql_gateway import SqlHosxpGateway

CONNECT_TIMEOUT_SECONDS = 10


def hosxp_engine(url: str) -> Engine:
    """Engine for the HOSxP database (read-only account), with a short connect timeout."""
    backend = url.split(":", 1)[0].split("+", 1)[0]
    if backend in ("mysql", "mariadb"):
        args: dict[str, object] = {"connect_timeout": CONNECT_TIMEOUT_SECONDS, "charset": "utf8mb4"}
    elif backend == "postgresql":
        args = {"connect_timeout": CONNECT_TIMEOUT_SECONDS}
    else:
        raise SettingsError(f"unsupported HOSxP database URL scheme {backend!r}")
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800, connect_args=args)


def build_gateway(settings: Settings) -> HosxpGateway:
    if settings.hosxp_mode == "mock":
        return MockHosxpGateway(sample_data())
    engine = hosxp_engine(settings.require_hosxp_database_url())
    return SqlHosxpGateway(engine, load_config(settings.hosxp_queries_file))
