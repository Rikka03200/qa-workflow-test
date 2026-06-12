"""DB-backed artifact mirror and materialization helpers.

Known ticket artifacts are mirrored into the platform DB with exact text, and DB
artifacts can be materialized back into a ticket tree for worker runs or a
per-user Web compatibility cache.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import config
from core.store.db import session_scope
from core.store.repositories import ArtifactRepository


ARTIFACT_NAMES = {
    "requirement.md",
    "business-context.md",
    "linked-issues.md",
    "analysis.md",
    "questions.md",
    "test-points.md",
    "_draft-design.json",
    "test-design.json",
    "_qa-packet.md",
    "_spot-check.md",
    "_revise.md",
}


def _relative_to(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None


def ticket_parts(path: Path) -> tuple[str, str, str, str, str] | None:
    """Return (owner, product, sprint, ticket_key, artifact_name) for a known artifact path."""
    artifact_name = path.name
    if artifact_name not in ARTIFACT_NAMES:
        return None

    rel = _relative_to(path, config.USERDATA_DIR)
    if rel is not None and len(rel.parts) >= 5 and rel.parts[1] == "tickets":
        owner, _, product, sprint, ticket_key = rel.parts[:5]
        return owner, product, sprint, ticket_key, artifact_name

    rel = _relative_to(path, config.TICKETS_DIR)
    if rel is not None and len(rel.parts) >= 4:
        product, sprint, ticket_key = rel.parts[:3]
        return "", product, sprint, ticket_key, artifact_name

    return None


def mirror_file(path: Path, *, title: str = "", owner_username: str | None = None) -> bool:
    """Best-effort mirror of a known artifact file into the platform DB."""
    parts = ticket_parts(path)
    engine = config.platform_engine()
    if parts is None or engine is None or not path.exists():
        return False
    owner, product, sprint, ticket_key, name = parts
    if owner_username is not None:
        owner = owner_username
    try:
        content = path.read_text(encoding="utf-8")
        with session_scope(engine) as session:
            repo = ArtifactRepository(session)
            repo.upsert_artifact(
                product_key=product,
                sprint=sprint,
                ticket_key=ticket_key,
                owner_username=owner,
                name=name,
                content_text=content,
                title=title,
                materialized_path=str(path),
            )
        return True
    except Exception:
        return False


def mirror_tree(root: Path) -> int:
    """Best-effort mirror of known artifact files under a ticket tree."""
    if not root.exists():
        return 0
    count = 0
    for path in root.rglob("*"):
        if path.is_file() and path.name in ARTIFACT_NAMES and mirror_file(path):
            count += 1
    return count


def mirror_materialized_tree(root: Path, *, owner_username: str) -> int:
    """Mirror artifacts from a materialized .work tree back to the platform DB."""
    engine = config.platform_engine()
    ticket_root = root / "tickets"
    if engine is None or not ticket_root.exists():
        return 0
    count = 0
    try:
        with session_scope(engine) as session:
            repo = ArtifactRepository(session)
            for path in ticket_root.rglob("*"):
                if not path.is_file() or path.name not in ARTIFACT_NAMES:
                    continue
                rel = _relative_to(path, ticket_root)
                if rel is None or len(rel.parts) < 4:
                    continue
                product, sprint, ticket_key = rel.parts[:3]
                repo.upsert_artifact(
                    product_key=product,
                    sprint=sprint,
                    ticket_key=ticket_key,
                    owner_username=owner_username,
                    name=path.name,
                    content_text=path.read_text(encoding="utf-8"),
                    materialized_path=str(path),
                )
                count += 1
    except Exception:
        return count
    return count


def work_root(job_id: str) -> Path:
    safe = "".join(ch for ch in str(job_id) if ch.isalnum() or ch in {"-", "_"})[:80] or "run"
    return config.REPO_ROOT / ".work" / safe


def materialize_ticket(*, owner_username: str, product: str, sprint: str, ticket_key: str, root: Path | None = None) -> Path | None:
    """Export all DB artifacts for one ticket into a file-mode ticket directory."""
    engine = config.platform_engine()
    if engine is None:
        return None
    root = root or config.REPO_ROOT / ".work" / "materialized"
    ticket_dir = root / "tickets" / product / sprint / ticket_key
    try:
        with session_scope(engine) as session:
            repo = ArtifactRepository(session)
            rows = repo.list_artifacts(product_key=product, sprint=sprint, ticket_key=ticket_key, owner_username=owner_username)
            if not rows:
                return None
            ticket_dir.mkdir(parents=True, exist_ok=True)
            for artifact in rows:
                if artifact.name not in ARTIFACT_NAMES:
                    continue
                target = ticket_dir / artifact.name
                tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
                tmp.write_text(artifact.content_text, encoding="utf-8")
                os.replace(tmp, target)
                artifact.materialized_path = str(target)
        return ticket_dir
    except Exception:
        return None


def materialize_sprint(*, owner_username: str, product: str, sprint: str, root: Path | None = None) -> tuple[Path, int]:
    """Export all DB-backed tickets for one sprint into a run work tree."""
    engine = config.platform_engine()
    root = root or config.REPO_ROOT / ".work" / "materialized"
    if engine is None:
        return root / "tickets", 0
    count = 0
    try:
        with session_scope(engine) as session:
            repo = ArtifactRepository(session)
            db_tickets = repo.list_tickets(product_key=product, sprint=sprint, owner_username=owner_username)
            keys = [ticket.key for ticket in db_tickets]
        for key in keys:
            if materialize_ticket(
                owner_username=owner_username,
                product=product,
                sprint=sprint,
                ticket_key=key,
                root=root,
            ):
                count += 1
    except Exception:
        return root / "tickets", count
    return root / "tickets", count


def export_user_cache(*, owner_username: str, product: str, sprint: str) -> int:
    """Export DB artifacts into the user's compatibility ticket tree."""
    if not owner_username:
        return 0
    _, count = materialize_sprint(
        owner_username=owner_username,
        product=product,
        sprint=sprint,
        root=config.user_tickets_dir(owner_username).parent,
    )
    return count
