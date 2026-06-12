"""Small repositories used by webapp while legacy file mode remains available."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from . import models
from .crypto import CredentialCipher, EncryptedSecret


_SECRET_ASSIGN_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|pat|personal[_-]?token)(\s*[=:]\s*)([^\s&;,]+)")
_URL_PASSWORD_RE = re.compile(r"(://[^:/\s]+:)([^@\s]+)(@)")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{12,})")


def redact_secret(text: str) -> str:
    """Redact common secret shapes before writing DB-backed job logs."""
    value = str(text or "")
    value = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)
    value = _URL_PASSWORD_RE.sub(r"\1[REDACTED]\3", value)
    value = _BEARER_RE.sub(r"\1[REDACTED]", value)
    return value


class CredentialRepository:
    def __init__(self, session: Session, cipher: CredentialCipher) -> None:
        self.session = session
        self.cipher = cipher

    def get_credential(self, user: models.User, kind: str) -> models.UserCredential | None:
        return self.session.scalar(
            select(models.UserCredential).where(models.UserCredential.user_id == user.id, models.UserCredential.kind == kind)
        )

    def upsert_credential(
        self,
        user: models.User,
        kind: str,
        *,
        value: str | None = None,
        provider: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> models.UserCredential:
        cred = self.get_credential(user, kind)
        if cred is None:
            cred = models.UserCredential(user_id=user.id, kind=kind, encrypted_value=b"")
            self.session.add(cred)
        if provider is not None:
            cred.provider = provider
        if base_url is not None:
            cred.base_url = base_url
        if model is not None:
            cred.model = model
        if value is not None:
            encrypted: EncryptedSecret = self.cipher.encrypt(value)
            cred.encrypted_value = encrypted.ciphertext
            cred.key_version = encrypted.key_version
        if metadata is not None:
            cred.metadata_json = {**(cred.metadata_json or {}), **metadata}
        return cred

    def set_secret(self, user: models.User, kind: str, value: str, *, provider: str = "", base_url: str = "", model: str = "") -> models.UserCredential:
        return self.upsert_credential(user, kind, value=value, provider=provider, base_url=base_url, model=model)

    def get_secret(self, user: models.User, kind: str) -> str:
        cred = self.get_credential(user, kind)
        return self.cipher.decrypt(cred.encrypted_value) if cred else ""


class JobRunRepository:
    """Persist and read pipeline run state in the platform database."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def ensure_product(self, product_key: str, display_name: str | None = None) -> models.Product:
        product = self.session.get(models.Product, product_key)
        if product is None:
            product = models.Product(key=product_key, display_name=display_name or product_key.upper())
            self.session.add(product)
            self.session.flush()
        return product

    def start_run(self, *, legacy_job_id: str, type_: str, product_key: str, sprint: str, label: str, owner_username: str, lock_key: str, argv_display: str = "", queue_name: str = "generation", status: str = "running", metadata: dict[str, Any] | None = None) -> models.PipelineRun:
        self.ensure_product(product_key)
        run = self.session.scalar(select(models.PipelineRun).where(models.PipelineRun.legacy_job_id == legacy_job_id))
        if run is None:
            run = models.PipelineRun(legacy_job_id=legacy_job_id, product_key=product_key)
            self.session.add(run)
        run.type = type_
        run.status = status
        run.sprint = sprint or ""
        run.label = label or ""
        run.owner_username = owner_username or ""
        run.lock_key = lock_key or ""
        run.argv_display = argv_display or ""
        run.queue_name = queue_name
        if status == "running":
            run.started_at = run.started_at or datetime.utcnow()
        run.metadata_json = metadata or {}
        self.session.flush()
        return run

    def append_log(self, legacy_job_id: str, line: str, *, stream: str = "stdout") -> None:
        run = self.get_run(legacy_job_id)
        if run is None:
            return
        max_seq = self.session.scalar(select(models.JobLog.seq).where(models.JobLog.run_id == run.id).order_by(models.JobLog.seq.desc()).limit(1)) or 0
        self.session.add(models.JobLog(run_id=run.id, seq=max_seq + 1, stream=stream, line=redact_secret(line)))
        run.updated_at = datetime.utcnow()

    def finish_run(self, legacy_job_id: str, *, status: str, rc: int | None) -> None:
        run = self.get_run(legacy_job_id)
        if run is None:
            return
        run.status = status
        run.rc = rc
        run.finished_at = datetime.utcnow()

    def get_run(self, legacy_job_id: str, *, owner_username: str | None = None) -> models.PipelineRun | None:
        stmt = select(models.PipelineRun).where(models.PipelineRun.legacy_job_id == legacy_job_id)
        if owner_username is not None:
            stmt = stmt.where(models.PipelineRun.owner_username == owner_username)
        return self.session.scalar(stmt)

    def list_runs(self, *, limit: int = 20, product_key: str | None = None, owner_username: str | None = None) -> list[models.PipelineRun]:
        stmt = select(models.PipelineRun)
        if product_key:
            stmt = stmt.where(models.PipelineRun.product_key == product_key)
        if owner_username:
            stmt = stmt.where(models.PipelineRun.owner_username == owner_username)
        stmt = stmt.order_by(models.PipelineRun.created_at.desc()).limit(limit)
        return list(self.session.scalars(stmt).all())

    def logs(self, legacy_job_id: str, *, limit: int = 2000, owner_username: str | None = None) -> list[str]:
        entries = self.log_entries_after(legacy_job_id, after_seq=0, limit=limit, owner_username=owner_username)
        return [entry["line"] for entry in entries]

    def log_entries_after(
        self,
        legacy_job_id: str,
        *,
        after_seq: int = 0,
        limit: int = 2000,
        owner_username: str | None = None,
    ) -> list[dict[str, Any]]:
        run = self.get_run(legacy_job_id, owner_username=owner_username)
        if run is None:
            return []
        stmt = (
            select(models.JobLog.seq, models.JobLog.stream, models.JobLog.line)
            .where(models.JobLog.run_id == run.id, models.JobLog.seq > after_seq)
            .order_by(models.JobLog.seq.asc())
            .limit(limit)
        )
        return [
            {"seq": int(seq), "stream": str(stream), "line": str(line)}
            for seq, stream, line in self.session.execute(stmt).all()
        ]

    def mark_stalled(self, *, older_than: datetime) -> int:
        runs = self.session.scalars(
            select(models.PipelineRun).where(models.PipelineRun.status.in_(["queued", "running"]), models.PipelineRun.updated_at < older_than)
        ).all()
        for run in runs:
            run.status = "stalled"
            run.finished_at = datetime.utcnow()
        return len(runs)

    def queue_depths(self) -> dict[str, int]:
        rows = self.session.execute(
            select(models.PipelineRun.queue_name, func.count(models.PipelineRun.id))
            .where(models.PipelineRun.status == "queued")
            .group_by(models.PipelineRun.queue_name)
        ).all()
        return {str(queue or "default"): int(count) for queue, count in rows}


class ArtifactRepository:
    """Store artifacts as exact text, preserving `test-design.json` byte-level semantics."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def ensure_product(self, product_key: str, display_name: str | None = None) -> models.Product:
        product = self.session.get(models.Product, product_key)
        if product is None:
            product = models.Product(key=product_key, display_name=display_name or product_key.upper())
            self.session.add(product)
            self.session.flush()
        return product

    def get_ticket(self, *, product_key: str, sprint: str, ticket_key: str, owner_username: str) -> models.Ticket | None:
        return self.session.scalar(
            select(models.Ticket).where(
                models.Ticket.product_key == product_key,
                models.Ticket.sprint == sprint,
                models.Ticket.key == ticket_key,
                models.Ticket.owner_username == owner_username,
            )
        )

    def upsert_artifact(self, *, product_key: str, sprint: str, ticket_key: str, owner_username: str, name: str, content_text: str, title: str = "", materialized_path: str = "") -> models.Artifact:
        self.ensure_product(product_key)
        ticket = self.get_ticket(product_key=product_key, sprint=sprint, ticket_key=ticket_key, owner_username=owner_username)
        if ticket is None:
            ticket = models.Ticket(product_key=product_key, sprint=sprint, key=ticket_key, owner_username=owner_username, title=title)
            self.session.add(ticket)
            self.session.flush()
        elif title:
            ticket.title = title
        artifact = self.session.scalar(
            select(models.Artifact).where(models.Artifact.ticket_id == ticket.id, models.Artifact.name == name)
        )
        content_hash = self.hash_text(content_text)
        if artifact is None:
            artifact = models.Artifact(ticket_id=ticket.id, name=name, rev=0)
            self.session.add(artifact)
        if artifact.content_hash != content_hash or artifact.content_text != content_text:
            artifact.content_text = content_text
            artifact.content_hash = content_hash
            artifact.rev += 1
        if materialized_path:
            artifact.materialized_path = materialized_path
        return artifact

    def list_tickets(self, *, product_key: str, sprint: str, owner_username: str) -> list[models.Ticket]:
        return list(
            self.session.scalars(
                select(models.Ticket)
                .where(
                    models.Ticket.product_key == product_key,
                    models.Ticket.sprint == sprint,
                    models.Ticket.owner_username == owner_username,
                )
                .order_by(models.Ticket.key.asc())
            ).all()
        )

    def list_sprints(self, *, product_key: str, owner_username: str) -> list[str]:
        stmt = (
            select(models.Ticket.sprint)
            .where(models.Ticket.product_key == product_key, models.Ticket.owner_username == owner_username)
            .distinct()
            .order_by(models.Ticket.sprint.desc())
        )
        return [str(sprint) for sprint in self.session.scalars(stmt).all()]

    def find_ticket(self, *, product_key: str, ticket_key: str, owner_username: str) -> models.Ticket | None:
        return self.session.scalar(
            select(models.Ticket)
            .where(
                models.Ticket.product_key == product_key,
                models.Ticket.key == ticket_key,
                models.Ticket.owner_username == owner_username,
            )
            .order_by(models.Ticket.sprint.desc())
            .limit(1)
        )

    def list_artifacts(self, *, product_key: str, sprint: str, ticket_key: str, owner_username: str) -> list[models.Artifact]:
        ticket = self.get_ticket(product_key=product_key, sprint=sprint, ticket_key=ticket_key, owner_username=owner_username)
        if ticket is None:
            return []
        return list(
            self.session.scalars(
                select(models.Artifact).where(models.Artifact.ticket_id == ticket.id).order_by(models.Artifact.name.asc())
            ).all()
        )


class OptionalJobRunMirror:
    """Best-effort DB mirror for legacy in-memory jobs."""

    def __init__(self, engine: Engine | None) -> None:
        self.engine = engine

    def enabled(self) -> bool:
        return self.engine is not None

    def _run(self, fn) -> None:
        if self.engine is None:
            return
        from .db import session_scope
        try:
            with session_scope(self.engine) as session:
                fn(JobRunRepository(session))
        except Exception:
            # The legacy in-memory job manager remains the serving truth during
            # rollout. DB mirror failures must not break generation.
            return

    def start(self, **kwargs) -> None:
        self._run(lambda repo: repo.start_run(**kwargs))

    def log(self, legacy_job_id: str, line: str) -> None:
        self._run(lambda repo: repo.append_log(legacy_job_id, line))

    def finish(self, legacy_job_id: str, *, status: str, rc: int | None) -> None:
        self._run(lambda repo: repo.finish_run(legacy_job_id, status=status, rc=rc))
