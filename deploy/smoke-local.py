#!/usr/bin/env python
"""Deployment smoke checks for a freshly installed qa-workflow environment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REQUIRED_TABLES = {
    "users",
    "sessions",
    "user_credentials",
    "products",
    "audit_events",
    "pipeline_runs",
    "pipeline_steps",
    "job_logs",
    "tickets",
    "artifacts",
    "coverage_ledger",
    "sprint_selections",
}
REQUIRED_MODULES = [
    "fastapi",
    "uvicorn",
    "jinja2",
    "itsdangerous",
    "multipart",
    "yaml",
    "psycopg",
    "sqlalchemy",
    "alembic",
    "procrastinate",
    "cryptography",
    "anthropic",
    "openai",
]


def _run(args: list[str], env: dict[str, str] | None = None) -> None:
    print("$", " ".join(args))
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def check_imports() -> None:
    for name in REQUIRED_MODULES:
        __import__(name)
    print("imports=ok")


def check_crypto() -> None:
    from core.store.crypto import CredentialCipher, generate_key

    cipher = CredentialCipher([generate_key()])
    encrypted = cipher.encrypt("secret-value")
    assert encrypted.ciphertext != b"secret-value"
    assert cipher.decrypt(encrypted.ciphertext) == "secret-value"
    print("fernet=ok")


def check_web_health(env: dict[str, str]) -> None:
    from fastapi.testclient import TestClient
    from webapp.main import app

    response = TestClient(app).get("/healthz")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert "platform_db" in payload
    assert "connection info string" not in payload.get("kb_reason", "")
    print("healthz=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))


def check_worker_import(env: dict[str, str]) -> None:
    from worker.app import QUEUES, _psycopg_conninfo

    assert set(QUEUES) == {"generation", "review", "maintenance"}
    assert _psycopg_conninfo("postgresql+psycopg://u:p@db/app") == "postgresql://u:p@db/app"
    print("worker=ok")


def check_migration_sqlite(env: dict[str, str]) -> str:
    from sqlalchemy import create_engine, inspect, text

    db = ROOT / ".work" / "platform-smoke.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    local_env = dict(env)
    local_env["QA_WEBAPP_DATABASE_URL"] = f"sqlite:///{db.as_posix()}"
    _run([sys.executable, "-m", "alembic", "-c", "deploy/alembic.ini", "upgrade", "head"], env=local_env)
    engine = create_engine(local_env["QA_WEBAPP_DATABASE_URL"], future=True)
    tables = set(inspect(engine).get_table_names())
    missing = sorted(REQUIRED_TABLES - tables)
    assert not missing, missing
    with engine.connect() as conn:
        product = conn.execute(text("select key from products where key='wms'")).scalar()
    assert product == "wms"
    print("migration_sqlite=ok")
    return local_env["QA_WEBAPP_DATABASE_URL"]


def check_auth_db(database_url: str) -> None:
    os.environ["QA_WEBAPP_DATABASE_URL"] = database_url
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from core.store import models
    from webapp import auth, config

    config.platform_engine.cache_clear()
    engine = create_engine(database_url, future=True)
    salt, pwd_hash = auth.hash_password("smoke-password")
    auth.store.upsert(auth.User(username="smoke", display_name="Smoke", role="admin", salt=salt, pwd_hash=pwd_hash, jira_pat="smoke-jira-secret"))
    user = auth.store.authenticate("smoke", "smoke-password")
    assert user is not None
    assert user.jira_pat == "smoke-jira-secret"
    token = auth.issue_session("smoke", user_agent="smoke", ip_address="127.0.0.1")
    assert auth.read_session(token) == "smoke"
    auth.revoke_session(token)
    assert auth.read_session(token) is None
    with Session(engine) as session:
        db_user = session.scalar(select(models.User).where(models.User.username == "smoke"))
        assert db_user is not None
        assert db_user.last_login_at is not None
    print("auth_db=ok")


def main() -> int:
    env = dict(os.environ)
    env.setdefault("QA_WEBAPP_DATABASE_URL", "sqlite:///:memory:")
    env.setdefault("QA_WEBAPP_SECRET", "smoke-test-secret")
    if not env.get("QA_FERNET_KEYS"):
        from cryptography.fernet import Fernet
        env["QA_FERNET_KEYS"] = Fernet.generate_key().decode("ascii")
    os.environ.update(env)

    check_imports()
    check_crypto()
    database_url = check_migration_sqlite(env)
    check_auth_db(database_url)
    check_web_health(env)
    check_worker_import(env)
    print("smoke=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
