"""platform foundation schema

Revision ID: 0001_platform_foundation
Revises:
Create Date: 2026-06-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_platform_foundation"
down_revision = None
branch_labels = None
depends_on = None


def _json_type():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _uuid_type():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


def upgrade() -> None:
    json_t = _json_type()
    uuid_t = _uuid_type()

    op.create_table(
        "products",
        sa.Column("key", sa.String(length=40), primary_key=True),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("ticket_key_regex", sa.String(length=200), nullable=False, server_default=r"^[A-Z]+-\d+$"),
        sa.Column("ticket_dir_glob", sa.String(length=80), nullable=False, server_default="*"),
        sa.Column("config", json_t, nullable=False, server_default=sa.text("'{}'::jsonb") if str(json_t).upper() == "JSONB" else None),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "users",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("display_name", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="qa"),
        sa.Column("password_salt", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("dingtalk_userid", sa.String(length=128), nullable=True),
        sa.Column("dingtalk_unionid", sa.String(length=128), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("role in ('admin','qa','viewer')", name="ck_users_role"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index("ix_users_username", "users", ["username"])

    op.create_table(
        "sessions",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("user_id", uuid_t, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False, unique=True),
        sa.Column("user_agent", sa.Text(), nullable=False, server_default=""),
        sa.Column("ip_address", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "user_credentials",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("user_id", uuid_t, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("base_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("metadata", json_t, nullable=False, server_default=sa.text("'{}'::jsonb") if str(json_t).upper() == "JSONB" else None),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind in ('jira','weak','strong','confluence')", name="ck_user_credentials_kind"),
        sa.UniqueConstraint("user_id", "kind", name="uq_user_credentials_user_kind"),
    )
    op.create_index("ix_user_credentials_user_id", "user_credentials", ["user_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("actor_user_id", uuid_t, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_username", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("product_key", sa.String(length=40), sa.ForeignKey("products.key", ondelete="SET NULL"), nullable=True),
        sa.Column("object_kind", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("object_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("detail", json_t, nullable=False, server_default=sa.text("'{}'::jsonb") if str(json_t).upper() == "JSONB" else None),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_actor_user_id", "audit_events", ["actor_user_id"])
    op.create_index("ix_audit_events_product_key", "audit_events", ["product_key"])

    op.create_table(
        "pipeline_runs",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("legacy_job_id", sa.String(length=40), nullable=False, unique=True),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
        sa.Column("product_key", sa.String(length=40), sa.ForeignKey("products.key", ondelete="RESTRICT"), nullable=False),
        sa.Column("sprint", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner_username", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("queue_name", sa.String(length=40), nullable=False, server_default="generation"),
        sa.Column("lock_key", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("argv_display", sa.Text(), nullable=False, server_default=""),
        sa.Column("rc", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", json_t, nullable=False, server_default=sa.text("'{}'::jsonb") if str(json_t).upper() == "JSONB" else None),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status in ('queued','running','done','failed','cancelled','stalled')", name="ck_pipeline_runs_status"),
    )
    op.create_index("ix_pipeline_runs_legacy_job_id", "pipeline_runs", ["legacy_job_id"])
    op.create_index("ix_pipeline_runs_product_key", "pipeline_runs", ["product_key"])
    op.create_index("ix_pipeline_runs_owner_product", "pipeline_runs", ["owner_username", "product_key"])

    op.create_table(
        "pipeline_steps",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("run_id", uuid_t, sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", json_t, nullable=False, server_default=sa.text("'{}'::jsonb") if str(json_t).upper() == "JSONB" else None),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_pipeline_steps_run_id", "pipeline_steps", ["run_id"])

    op.create_table(
        "job_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", uuid_t, sa.ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("stream", sa.String(length=16), nullable=False, server_default="stdout"),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("run_id", "seq", name="uq_job_logs_run_seq"),
    )
    op.create_index("ix_job_logs_run_id", "job_logs", ["run_id"])

    op.create_table(
        "tickets",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("product_key", sa.String(length=40), sa.ForeignKey("products.key", ondelete="RESTRICT"), nullable=False),
        sa.Column("sprint", sa.String(length=40), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("owner_username", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="jira"),
        sa.Column("metadata", json_t, nullable=False, server_default=sa.text("'{}'::jsonb") if str(json_t).upper() == "JSONB" else None),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("product_key", "sprint", "key", "owner_username", name="uq_tickets_product_sprint_key_owner"),
    )
    op.create_index("ix_tickets_product_key", "tickets", ["product_key"])
    op.create_index("ix_tickets_sprint", "tickets", ["sprint"])
    op.create_index("ix_tickets_key", "tickets", ["key"])

    op.create_table(
        "artifacts",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("ticket_id", uuid_t, sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("rev", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("kind", sa.String(length=40), nullable=False, server_default="text"),
        sa.Column("materialized_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("ticket_id", "name", name="uq_artifacts_ticket_name"),
    )
    op.create_index("ix_artifacts_ticket_id", "artifacts", ["ticket_id"])

    op.create_table(
        "coverage_ledger",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("product_key", sa.String(length=40), sa.ForeignKey("products.key", ondelete="CASCADE"), nullable=False),
        sa.Column("feature_key", sa.String(length=80), nullable=False),
        sa.Column("covered_by_key", sa.String(length=80), nullable=False),
        sa.Column("sprint", sa.String(length=40), nullable=False),
        sa.Column("platform", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="run"),
        sa.Column("owner_username", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("product_key", "feature_key", "owner_username", name="uq_coverage_feature_owner"),
    )
    op.create_index("ix_coverage_ledger_product_key", "coverage_ledger", ["product_key"])

    op.create_table(
        "sprint_selections",
        sa.Column("id", uuid_t, primary_key=True),
        sa.Column("product_key", sa.String(length=40), sa.ForeignKey("products.key", ondelete="CASCADE"), nullable=False),
        sa.Column("sprint", sa.String(length=40), nullable=False),
        sa.Column("owner_username", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("payload", json_t, nullable=False, server_default=sa.text("'{}'::jsonb") if str(json_t).upper() == "JSONB" else None),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("product_key", "sprint", "owner_username", name="uq_sprint_selection_owner"),
    )
    op.create_index("ix_sprint_selections_product_key", "sprint_selections", ["product_key"])

    product_seed = sa.table(
        "products",
        sa.column("key", sa.String(length=40)),
        sa.column("display_name", sa.String(length=120)),
        sa.column("ticket_key_regex", sa.String(length=200)),
        sa.column("ticket_dir_glob", sa.String(length=80)),
        sa.column("config", json_t),
    )
    op.bulk_insert(product_seed, [
        {"key": "wms", "display_name": "WMS", "ticket_key_regex": r"^[A-Z]+-\d+$", "ticket_dir_glob": "EAR-*", "config": {"seed": "0001"}},
    ])


def downgrade() -> None:
    for table in [
        "sprint_selections",
        "coverage_ledger",
        "artifacts",
        "tickets",
        "job_logs",
        "pipeline_steps",
        "pipeline_runs",
        "audit_events",
        "user_credentials",
        "sessions",
        "users",
        "products",
    ]:
        op.drop_table(table)
