"""Best-effort Jira issue snapshot cache backed by the platform DB."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from core.productcfg import DEFAULT_PRODUCT
from core.store import models
from core.store.db import engine_from_url, session_scope
from core.store.repositories import ArtifactRepository


def _ttl_seconds() -> int:
    raw = os.environ.get("QA_JIRA_CACHE_TTL_SECONDS", "900")
    try:
        value = int(raw)
    except ValueError:
        return 900
    return max(0, value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_cached_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _product_key(env: dict[str, str] | None = None) -> str:
    return (env or {}).get("QA_PRODUCT") or os.environ.get("QA_PRODUCT") or DEFAULT_PRODUCT


def enabled(env: dict[str, str] | None = None) -> bool:
    if (env or {}).get("QA_JIRA_CACHE_DISABLE") or os.environ.get("QA_JIRA_CACHE_DISABLE"):
        return False
    return _ttl_seconds() > 0 and engine_from_url() is not None


def load_issue(key: str, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    if not enabled(env):
        return None
    engine = engine_from_url()
    if engine is None:
        return None
    try:
        with session_scope(engine) as session:
            ticket = session.scalar(
                select(models.Ticket)
                .where(models.Ticket.product_key == _product_key(env), models.Ticket.key == key)
                .order_by(models.Ticket.updated_at.desc())
                .limit(1)
            )
            if ticket is None:
                return None
            cache = (ticket.metadata_json or {}).get("jira_snapshot") or {}
            cached_at = _parse_cached_at(str(cache.get("cached_at") or ""))
            if cached_at is None or _now() - cached_at > timedelta(seconds=_ttl_seconds()):
                return None
            issue = cache.get("issue")
            return issue if isinstance(issue, dict) else None
    except SQLAlchemyError:
        return None


def save_issue(issue: dict[str, Any], *, env: dict[str, str] | None = None, owner_username: str = "") -> bool:
    key = str(issue.get("key") or "")
    if not key:
        return False
    engine = engine_from_url()
    if engine is None:
        return False
    fields = issue.get("fields") or {}
    title = str(fields.get("summary") or "")
    product_key = _product_key(env)
    try:
        with session_scope(engine) as session:
            repo = ArtifactRepository(session)
            repo.ensure_product(product_key)
            ticket = session.scalar(
                select(models.Ticket).where(
                    models.Ticket.product_key == product_key,
                    models.Ticket.sprint == "_jira-cache",
                    models.Ticket.key == key,
                    models.Ticket.owner_username == owner_username,
                )
            )
            if ticket is None:
                ticket = models.Ticket(product_key=product_key, sprint="_jira-cache", key=key, owner_username=owner_username)
                session.add(ticket)
            ticket.title = title
            ticket.source = "jira"
            ticket.metadata_json = {
                **(ticket.metadata_json or {}),
                "jira_snapshot": {"cached_at": _now().isoformat(), "issue": issue},
            }
        return True
    except SQLAlchemyError:
        return False
