"""SQLAlchemy ORM models for qa-workflow platform data.

These models are the PostgreSQL target schema for the scale-up plan. They are
kept free of application side effects so Alembic, webapp and worker processes
can import metadata safely.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GUID(TypeDecorator):
    """Platform-independent UUID type."""

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class JSONCompat(TypeDecorator):
    """Use JSONB on PostgreSQL and JSON elsewhere for fast local tests."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB)
        return dialect.type_descriptor(JSON)


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dict[str, Any]: JSONCompat}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now(), nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="qa")
    password_salt: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    dingtalk_userid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dingtalk_unionid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    credentials: Mapped[list["UserCredential"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("role in ('admin','qa','viewer')", name="ck_users_role"),
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    user_agent: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ip_address: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class UserCredential(Base, TimestampMixin):
    __tablename__ = "user_credentials"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    base_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")
    key_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONCompat(), nullable=False, default=dict)

    user: Mapped[User] = relationship(back_populates="credentials")

    __table_args__ = (
        UniqueConstraint("user_id", "kind", name="uq_user_credentials_user_kind"),
        CheckConstraint("kind in ('jira','weak','strong','confluence')", name="ck_user_credentials_kind"),
    )


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    ticket_key_regex: Mapped[str] = mapped_column(String(200), nullable=False, default=r"^[A-Z]+-\d+$")
    ticket_dir_glob: Mapped[str] = mapped_column(String(80), nullable=False, default="*")
    config: Mapped[dict[str, Any]] = mapped_column(JSONCompat(), nullable=False, default=dict)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_username: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    product_key: Mapped[str | None] = mapped_column(String(40), ForeignKey("products.key", ondelete="SET NULL"), nullable=True, index=True)
    object_kind: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    object_id: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONCompat(), nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False)


class PipelineRun(Base, TimestampMixin):
    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    legacy_job_id: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    product_key: Mapped[str] = mapped_column(String(40), ForeignKey("products.key", ondelete="RESTRICT"), nullable=False, index=True)
    sprint: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    owner_username: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    queue_name: Mapped[str] = mapped_column(String(40), nullable=False, default="generation")
    lock_key: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    argv_display: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONCompat(), nullable=False, default=dict)

    steps: Mapped[list["PipelineStep"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    logs: Mapped[list["JobLog"]] = relationship(back_populates="run", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("status in ('queued','running','done','failed','cancelled','stalled')", name="ck_pipeline_runs_status"),
        Index("ix_pipeline_runs_owner_product", "owner_username", "product_key"),
    )


class PipelineStep(Base, TimestampMixin):
    __tablename__ = "pipeline_steps"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONCompat(), nullable=False, default=dict)

    run: Mapped[PipelineRun] = relationship(back_populates="steps")


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    stream: Mapped[str] = mapped_column(String(16), nullable=False, default="stdout")
    line: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False)

    run: Mapped[PipelineRun] = relationship(back_populates="logs")

    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_job_logs_run_seq"),)


class Ticket(Base, TimestampMixin):
    __tablename__ = "tickets"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    product_key: Mapped[str] = mapped_column(String(40), ForeignKey("products.key", ondelete="RESTRICT"), nullable=False, index=True)
    sprint: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    owner_username: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="jira")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONCompat(), nullable=False, default=dict)

    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="ticket", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("product_key", "sprint", "key", "owner_username", name="uq_tickets_product_sprint_key_owner"),
    )


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rev: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="text")
    materialized_path: Mapped[str] = mapped_column(Text, nullable=False, default="")

    ticket: Mapped[Ticket] = relationship(back_populates="artifacts")

    __table_args__ = (UniqueConstraint("ticket_id", "name", name="uq_artifacts_ticket_name"),)


class CoverageLedger(Base, TimestampMixin):
    __tablename__ = "coverage_ledger"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    product_key: Mapped[str] = mapped_column(String(40), ForeignKey("products.key", ondelete="CASCADE"), nullable=False, index=True)
    feature_key: Mapped[str] = mapped_column(String(80), nullable=False)
    covered_by_key: Mapped[str] = mapped_column(String(80), nullable=False)
    sprint: Mapped[str] = mapped_column(String(40), nullable=False)
    platform: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="run")
    owner_username: Mapped[str] = mapped_column(String(80), nullable=False, default="")

    __table_args__ = (UniqueConstraint("product_key", "feature_key", "owner_username", name="uq_coverage_feature_owner"),)


class SprintSelection(Base, TimestampMixin):
    __tablename__ = "sprint_selections"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    product_key: Mapped[str] = mapped_column(String(40), ForeignKey("products.key", ondelete="CASCADE"), nullable=False, index=True)
    sprint: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_username: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONCompat(), nullable=False, default=dict)

    __table_args__ = (UniqueConstraint("product_key", "sprint", "owner_username", name="uq_sprint_selection_owner"),)
