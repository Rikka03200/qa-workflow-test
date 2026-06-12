"""Database engine/session helpers for qa-workflow platform store."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker


def database_url(default: str | None = None) -> str | None:
    """Resolve the WebApp platform database URL without reading secret files.

    `QA_WEBAPP_DATABASE_URL` is preferred over the generic names so the platform
    DB can be separated from the existing KB database.
    """
    for key in ("QA_WEBAPP_DATABASE_URL", "QA_DATABASE_URL", "DATABASE_URL"):
        value = os.environ.get(key)
        if value:
            return value
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def engine_pool_options(url: str) -> dict:
    """Return SQLAlchemy pool options for persistent PostgreSQL engines."""
    dialect = make_url(url).get_backend_name()
    if not dialect.startswith("postgresql"):
        return {}
    return {
        "pool_size": _env_int("QA_DB_POOL_SIZE", 5),
        "max_overflow": _env_int("QA_DB_MAX_OVERFLOW", 5),
        "pool_recycle": _env_int("QA_DB_POOL_RECYCLE", 3600),
    }


def engine_from_url(url: str | None = None, **kwargs) -> Engine | None:
    url = url or database_url()
    if not url:
        return None
    options = {"pool_pre_ping": True, "future": True, **engine_pool_options(url), **kwargs}
    return create_engine(url, **options)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    factory = session_factory(engine)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check(engine: Engine | None) -> tuple[bool, str]:
    if engine is None:
        return False, "not configured"
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
