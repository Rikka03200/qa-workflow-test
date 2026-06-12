"""Idempotent Procrastinate schema bootstrap for Docker Compose."""

from __future__ import annotations

import psycopg
from procrastinate.schema import SchemaManager

from core.store.db import database_url

from .app import _psycopg_conninfo, app


_REQUIRED_TABLES = {
    "procrastinate_jobs",
    "procrastinate_events",
    "procrastinate_workers",
    "procrastinate_periodic_defers",
}


def _existing_tables() -> set[str]:
    url = database_url()
    if not url:
        raise RuntimeError("未配置 QA_WEBAPP_DATABASE_URL，无法初始化 Procrastinate schema")
    with psycopg.connect(_psycopg_conninfo(url)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select tablename
                from pg_tables
                where schemaname = 'public'
                  and tablename like 'procrastinate_%'
                """
            )
            return {row[0] for row in cursor.fetchall()}


def ensure_schema() -> bool:
    """Apply Procrastinate schema only when it has not been installed yet."""
    existing = _existing_tables()
    if _REQUIRED_TABLES <= existing:
        return False
    with app.open():
        SchemaManager(app.connector).apply_schema()
    return True


def main() -> int:
    applied = ensure_schema()
    print("procrastinate_schema=" + ("applied" if applied else "exists"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
