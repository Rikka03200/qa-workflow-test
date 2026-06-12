"""Procrastinate app factory.

The webapp still serves legacy in-memory jobs during rollout. This module only
provides the queue foundation required by the scale-up plan and fails loudly when
the optional worker dependencies are not installed.
"""

from __future__ import annotations

from core.store.db import _env_int, database_url

QUEUES = ("generation", "review", "maintenance")


class WorkerSetupError(RuntimeError):
    pass


def require_procrastinate():
    try:
        import procrastinate
        return procrastinate
    except Exception as exc:  # noqa: BLE001
        raise WorkerSetupError("缺少 procrastinate；请先安装 webapp/requirements-web.txt") from exc


def _psycopg_conninfo(url: str) -> str:
    """Procrastinate uses psycopg directly; strip SQLAlchemy's psycopg dialect marker."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def connector_pool_options() -> dict:
    """Return psycopg_pool options for Procrastinate's connector."""
    return {
        "min_size": _env_int("QA_QUEUE_POOL_MIN_SIZE", 1),
        "max_size": _env_int("QA_QUEUE_POOL_MAX_SIZE", 2),
        "max_lifetime": _env_int("QA_QUEUE_POOL_MAX_LIFETIME", 3600),
    }


def create_app():
    procrastinate = require_procrastinate()
    url = database_url()
    if not url:
        raise WorkerSetupError("未配置 QA_WEBAPP_DATABASE_URL，无法启动 worker")
    return procrastinate.App(
        connector=procrastinate.PsycopgConnector(conninfo=_psycopg_conninfo(url), **connector_pool_options())
    )


app = create_app()

# Import task declarations for `procrastinate --app=worker.app.app worker ...`.
try:
    from . import tasks  # noqa: F401,E402
except Exception:
    raise
