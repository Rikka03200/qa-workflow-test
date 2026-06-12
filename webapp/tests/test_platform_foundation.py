import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from core.productcfg import from_raw_config
from core.store import models
from core.store.repositories import ArtifactRepository, JobRunRepository
from webapp import auth
from webapp.deps import subprocess_env


class _FakeLoader:
    @staticmethod
    def parse_config():
        return {
            "JIRA_URL": "https://jira.example.invalid",
            "JIRA_PERSONAL_TOKEN": "service-jira-token",
            "CHEAP_MODEL_API_KEY": "service-weak-token",
            "ANTHROPIC_API_KEY": "must-not-leak",
            "QA_WEBAPP_DATABASE_URL": "must-not-leak",
            "QA_FERNET_KEYS": "must-not-leak",
            "APIFOX_TOKEN": "must-not-leak",
            "DINGTALK_APP_SECRET": "must-not-leak",
        }


class _FakeCipher:
    key_version = "fake-v1"
    _store = {}

    def encrypt(self, value):
        if isinstance(value, str):
            value = value.encode("utf-8")
        token = f"cipher-{len(self._store) + 1}".encode("ascii")
        self._store[token] = value
        return type("Encrypted", (), {"ciphertext": token, "key_version": self.key_version})()

    def decrypt(self, value):
        if isinstance(value, str):
            value = value.encode("utf-8")
        return self._store[value].decode("utf-8")


def test_platform_metadata_contains_foundation_tables():
    assert {
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
    } <= set(models.Base.metadata.tables)


def test_platform_metadata_uses_stable_naming_convention():
    assert models.Base.metadata.naming_convention["fk"] == "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    check_names = {constraint.name for constraint in models.Base.metadata.tables["users"].constraints}
    assert "ck_users_role" in check_names
    assert "pk_users" in check_names


def test_product_config_accepts_nested_schema_and_legacy_defaults():
    pc = from_raw_config({
        "selection": {"tester": "Global QA"},
        "products": {
            "oms": {
                "display_name": "OMS",
                "jira": {"project_keys": ["OMS"], "board_id": 99},
                "output": {"ticket_key_regex": r"^OMS-\d+$", "ticket_dir_glob": "OMS-*"},
                "platforms": {"labels": {"mini": "小程序"}},
                "selection": {"tester": "Product QA"},
                "kb": {"path": "_kb/projects/oms"},
            }
        },
    }, "oms")
    assert pc.display_name == "OMS"
    assert pc.jira_project_keys == ("OMS",)
    assert pc.jira_board_id == 99
    assert pc.valid_ticket_key("OMS-123")
    assert not pc.valid_ticket_key("EAR-123")
    assert pc.ticket_glob() == "OMS-*"
    assert pc.platform_labels["mini"] == "小程序"
    assert pc.selection["tester"] == "Product QA"
    assert pc.kb_path == "_kb/projects/oms"


def test_subprocess_env_filters_platform_and_strong_secrets(monkeypatch):
    from webapp import deps

    monkeypatch.setenv("QA_WEBAPP_DATABASE_URL", "must-not-leak")
    monkeypatch.setenv("QA_FERNET_KEYS", "must-not-leak")
    monkeypatch.setenv("DINGTALK_APP_SECRET", "must-not-leak")
    monkeypatch.setenv("APIFOX_TOKEN", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("QA_JIRA_RATE_QPS", "2")
    monkeypatch.setenv("QA_JIRA_RATE_BURST", "4")
    monkeypatch.setenv("QA_JIRA_CACHE_TTL_SECONDS", "600")
    monkeypatch.setattr(deps.scripts_loader, "load_env", lambda: _FakeLoader)

    user = auth.User(
        username="linzixuan",
        display_name="林子宣",
        jira_pat="user-jira-token",
        ai={
            "weak": {"provider": "anthropic", "base_url": "https://weak.example.invalid", "model": "weak-model", "api_key": "user-weak-token"},
            "strong": {"provider": "openai", "base_url": "https://strong.example.invalid", "model": "strong-model", "api_key": "user-strong-token"},
        },
    )
    env = subprocess_env(user)

    assert env["JIRA_PERSONAL_TOKEN"] == "user-jira-token"
    assert env["CHEAP_MODEL_API_KEY"] == "user-weak-token"
    assert env["QA_TICKETS_ROOT"].replace("\\", "/").endswith("userdata/linzixuan/tickets")
    assert env["QA_SELECT_TESTER"] == "林子宣"
    assert env["QA_JIRA_RATE_QPS"] == "2"
    assert env["QA_JIRA_RATE_BURST"] == "4"
    assert env["QA_JIRA_CACHE_TTL_SECONDS"] == "600"
    for forbidden in ("QA_WEBAPP_DATABASE_URL", "QA_DATABASE_URL", "QA_FERNET_KEYS", "DINGTALK_APP_SECRET", "APIFOX_TOKEN", "ANTHROPIC_API_KEY"):
        assert forbidden not in env
    assert "user-strong-token" not in json.dumps(env, ensure_ascii=False)


def test_mcp_atlassian_wrapper_filters_unrelated_secrets():
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import importlib.util

    spec = importlib.util.spec_from_file_location("mcp_atlassian_wrapper", scripts_dir / "mcp-atlassian-wrapper.py")
    wrapper = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(wrapper)

    env = wrapper.subprocess_env(
        {"JIRA_URL": "https://jira.example.invalid", "JIRA_PERSONAL_TOKEN": "jira-token"},
        base={
            "PATH": "/bin",
            "QA_WEBAPP_DATABASE_URL": "must-not-leak",
            "QA_FERNET_KEYS": "must-not-leak",
            "ANTHROPIC_API_KEY": "must-not-leak",
            "APIFOX_TOKEN": "must-not-leak",
        },
    )
    assert env["PATH"] == "/bin"
    assert env["JIRA_PERSONAL_TOKEN"] == "jira-token"
    assert env["DISABLE_JIRA_MARKUP_TRANSLATION"] == "true"
    for forbidden in ("QA_WEBAPP_DATABASE_URL", "QA_FERNET_KEYS", "ANTHROPIC_API_KEY", "APIFOX_TOKEN"):
        assert forbidden not in env


def test_user_store_encrypts_json_secrets_with_configured_cipher(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_cipher_or_none", lambda: _FakeCipher())
    store = auth.UserStore(tmp_path / "users.json")
    salt, pwd_hash = auth.hash_password("pw")
    store.upsert(auth.User(
        username="u1",
        salt=salt,
        pwd_hash=pwd_hash,
        jira_pat="jira-secret",
        ai={"weak": {"api_key": "weak-secret"}, "strong": {"api_key": "strong-secret"}},
    ))

    raw = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))["users"][0]
    assert raw["jira_pat"].startswith("fernet:")
    assert raw["ai"]["weak"]["api_key"].startswith("fernet:")
    assert "jira-secret" not in json.dumps(raw, ensure_ascii=False)
    assert store.get("u1").jira_pat == "jira-secret"
    assert store.get("u1").ai["strong"]["api_key"] == "strong-secret"


def test_auth_cli_adduser_can_read_password_from_env(monkeypatch, tmp_path):
    store = auth.UserStore(tmp_path / "users.json")
    monkeypatch.setattr(auth, "store", store)
    monkeypatch.setenv("QA_TEST_BOOTSTRAP_PASSWORD", "local-test-password")
    monkeypatch.setattr(sys, "argv", [
        "webapp.auth",
        "adduser",
        "bootstrap",
        "--name",
        "Bootstrap User",
        "--role",
        "admin",
        "--password-env",
        "QA_TEST_BOOTSTRAP_PASSWORD",
    ])

    assert auth._cli() == 0
    user = store.authenticate("bootstrap", "local-test-password")
    assert user is not None
    assert user.display_name == "Bootstrap User"
    assert user.role == "admin"


def test_settings_page_and_secret_endpoint_do_not_return_plaintext(monkeypatch, tmp_path):
    from webapp.main import app

    monkeypatch.setattr(auth, "_cipher_or_none", lambda: None)
    store = auth.UserStore(tmp_path / "users.json")
    salt, pwd_hash = auth.hash_password("pw")
    store.upsert(auth.User(
        username="u2",
        salt=salt,
        pwd_hash=pwd_hash,
        jira_pat="jira-token-secret",
        jira_identity={"display_name": "Jira User", "verified_at": "2026-06-11T00:00:00+00:00"},
        ai={"weak": {"api_key": "weak-token-secret"}, "strong": {"api_key": "strong-token-secret"}},
    ))
    monkeypatch.setattr(auth, "store", store)

    client = TestClient(app)
    client.cookies.set("qa_session", auth.issue_session("u2"))
    html = client.get("/settings").text
    assert "jira-token-secret" not in html
    assert "weak-token-secret" not in html
    assert "strong-token-secret" not in html
    assert "明文不会回显" in html
    assert "最近验证：Jira User" in html

    resp = client.get("/settings/secret/weak")
    assert resp.status_code == 403
    assert "weak-token-secret" not in resp.text


def test_settings_test_jira_persists_nonsecret_identity_without_temp_pat(monkeypatch, tmp_path):
    from webapp.main import app
    from webapp.services import scripts_loader

    monkeypatch.setattr(auth, "_cipher_or_none", lambda: None)
    store = auth.UserStore(tmp_path / "users.json")
    salt, pwd_hash = auth.hash_password("pw")
    store.upsert(auth.User(username="u3", salt=salt, pwd_hash=pwd_hash))
    monkeypatch.setattr(auth, "store", store)

    class _JiraFetch:
        @staticmethod
        def myself(env):
            assert env["JIRA_URL"] == "https://jira.example.invalid"
            assert env["JIRA_PERSONAL_TOKEN"] == "temporary-token"
            return "Verified Jira User"

    monkeypatch.setattr(scripts_loader, "load_normal", lambda name: _JiraFetch)

    client = TestClient(app)
    client.cookies.set("qa_session", auth.issue_session("u3"))
    client.get("/settings")
    csrf = client.cookies.get("qa_csrf")
    resp = client.post(
        "/settings/test-jira",
        data={"jira_url": "https://jira.example.invalid/", "jira_pat": "temporary-token", "csrf_token": csrf},
    )

    assert resp.json()["ok"] is True
    loaded = store.get("u3")
    assert loaded is not None
    assert loaded.jira_pat == ""
    assert loaded.jira_identity["display_name"] == "Verified Jira User"
    assert loaded.jira_identity["base_url"] == "https://jira.example.invalid"
    raw = (tmp_path / "users.json").read_text(encoding="utf-8")
    assert "temporary-token" not in raw


def test_artifact_repository_preserves_text_and_revisions():
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine("sqlite:///:memory:", future=True)
    models.Base.metadata.create_all(engine)
    Session = sqlalchemy.orm.sessionmaker(bind=engine, future=True)
    with Session() as session:
        repo = ArtifactRepository(session)
        text = "{\n  \"raw\": \"bytes stay stable\"\n}\n"
        artifact = repo.upsert_artifact(
            product_key="wms", sprint="2026-06-11", ticket_key="EAR-1", owner_username="u1",
            name="test-design.json", content_text=text, title="Title", materialized_path="tickets/wms/EAR-1/test-design.json",
        )
        session.commit()
        assert artifact.content_text == text
        assert artifact.content_hash == ArtifactRepository.hash_text(text)
        assert artifact.materialized_path == "tickets/wms/EAR-1/test-design.json"
        assert artifact.rev == 1

        artifact = repo.upsert_artifact(
            product_key="wms", sprint="2026-06-11", ticket_key="EAR-1", owner_username="u1",
            name="test-design.json", content_text=text + " ", title="Title",
        )
        session.commit()
        assert artifact.content_text == text + " "
        assert artifact.rev == 2
        assert repo.list_artifacts(product_key="wms", sprint="2026-06-11", ticket_key="EAR-1", owner_username="u1") == [artifact]


def test_artifact_service_mirrors_and_materializes_whitelisted_files(monkeypatch, tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from webapp.services import artifacts

    engine = sqlalchemy.create_engine("sqlite:///:memory:", future=True)
    models.Base.metadata.create_all(engine)
    userdata = tmp_path / "userdata"
    tickets = tmp_path / "tickets"
    monkeypatch.setattr(artifacts.config, "USERDATA_DIR", userdata)
    monkeypatch.setattr(artifacts.config, "TICKETS_DIR", tickets)
    monkeypatch.setattr(artifacts.config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(artifacts.config, "platform_engine", lambda: engine)

    user_file = userdata / "u1" / "tickets" / "wms" / "2026-06-11" / "EAR-1" / "test-design.json"
    user_file.parent.mkdir(parents=True)
    text = "{\n  \"raw\": \"bytes stay stable\"\n}\n"
    user_file.write_text(text, encoding="utf-8")
    legacy_file = tickets / "wms" / "2026-06-11" / "EAR-2" / "questions.md"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text("# Questions\n\n无\n", encoding="utf-8")
    ignored = user_file.with_name("notes.txt")
    ignored.write_text("ignore", encoding="utf-8")

    assert artifacts.ticket_parts(user_file) == ("u1", "wms", "2026-06-11", "EAR-1", "test-design.json")
    assert artifacts.ticket_parts(legacy_file) == ("", "wms", "2026-06-11", "EAR-2", "questions.md")
    assert artifacts.ticket_parts(ignored) is None
    assert artifacts.mirror_file(ignored) is False
    assert artifacts.mirror_file(user_file) is True
    assert artifacts.mirror_file(legacy_file) is True

    with sqlalchemy.orm.Session(engine) as session:
        repo = ArtifactRepository(session)
        mirrored = repo.list_artifacts(product_key="wms", sprint="2026-06-11", ticket_key="EAR-1", owner_username="u1")
        assert len(mirrored) == 1
        assert mirrored[0].content_text == text
        assert mirrored[0].content_hash == ArtifactRepository.hash_text(text)
        assert mirrored[0].materialized_path == str(user_file)
        legacy = repo.list_artifacts(product_key="wms", sprint="2026-06-11", ticket_key="EAR-2", owner_username="")
        assert legacy[0].content_text == "# Questions\n\n无\n"

    target_dir = artifacts.materialize_ticket(
        owner_username="u1", product="wms", sprint="2026-06-11", ticket_key="EAR-1", root=tmp_path / "work"
    )
    assert target_dir == tmp_path / "work" / "tickets" / "wms" / "2026-06-11" / "EAR-1"
    assert (target_dir / "test-design.json").read_text(encoding="utf-8") == text

    assert artifacts.export_user_cache(owner_username="u1", product="wms", sprint="2026-06-11") == 1
    cached = userdata / "u1" / "tickets" / "wms" / "2026-06-11" / "EAR-1" / "test-design.json"
    assert cached.read_text(encoding="utf-8") == text


def test_ticket_service_materializes_db_artifacts_for_user_cache(monkeypatch, tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from webapp import config
    from webapp.services import tickets

    engine = sqlalchemy.create_engine("sqlite:///:memory:", future=True)
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(config, "platform_engine", lambda: engine)
    monkeypatch.setattr(config, "USERDATA_DIR", tmp_path / "userdata")
    monkeypatch.setattr(config, "TICKETS_DIR", tmp_path / "tickets")
    config.set_user_root("alice")
    try:
        with sqlalchemy.orm.Session(engine) as session:
            repo = ArtifactRepository(session)
            repo.upsert_artifact(
                product_key="wms",
                sprint="2026-06-11",
                ticket_key="EAR-1",
                owner_username="alice",
                name="questions.md",
                content_text="# Questions\n\n无\n",
            )
            session.commit()

        assert tickets.sprint_dates("wms") == ["2026-06-11"]
        found = tickets.find_ticket("wms", "EAR-1")
        assert found == tmp_path / "userdata" / "alice" / "tickets" / "wms" / "2026-06-11" / "EAR-1"
        assert (found / "questions.md").read_text(encoding="utf-8") == "# Questions\n\n无\n"
        assert tickets.list_ticket_dirs("wms", "2026-06-11") == [found]
    finally:
        config.set_user_root(None)


def test_run_sprint_display_path_handles_external_ticket_root():
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        import run_sprint
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"run_sprint dependencies unavailable: {type(exc).__name__}: {exc}")

    outside = Path("/tmp/qa-workflow-external/EAR-1")
    shown = run_sprint.display_path(outside)
    assert shown
    assert "EAR-1" in shown


def test_platform_engine_pool_options_use_postgres_budget(monkeypatch):
    from core.store import db

    monkeypatch.setenv("QA_DB_POOL_SIZE", "7")
    monkeypatch.setenv("QA_DB_MAX_OVERFLOW", "3")
    monkeypatch.setenv("QA_DB_POOL_RECYCLE", "1800")

    assert db.engine_pool_options("postgresql+psycopg://u:p@db:5432/app") == {
        "pool_size": 7,
        "max_overflow": 3,
        "pool_recycle": 1800,
    }
    assert db.engine_pool_options("sqlite:///:memory:") == {}


def test_jira_fetch_uses_rate_limit_and_snapshot_cache(monkeypatch, tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import jira_cache
    import jira_fetch

    db = tmp_path / "jira-cache.sqlite"
    engine = sqlalchemy.create_engine(f"sqlite:///{db.as_posix()}", future=True)
    models.Base.metadata.create_all(engine)
    monkeypatch.setenv("QA_WEBAPP_DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("QA_JIRA_CACHE_TTL_SECONDS", "900")
    monkeypatch.setenv("QA_JIRA_RATE_QPS", "1")
    monkeypatch.setenv("QA_JIRA_RATE_BURST", "1")
    monkeypatch.setattr(jira_cache, "engine_from_url", lambda: engine)

    calls = {"rate": 0, "urlopen": 0}
    issue = {"key": "EAR-1", "fields": {"summary": "Cached issue"}}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps(issue).encode("utf-8")

    def fake_urlopen(*args, **kwargs):
        calls["urlopen"] += 1
        return _Response()

    monkeypatch.setattr(jira_fetch, "_rate_limit", lambda: calls.__setitem__("rate", calls["rate"] + 1))
    monkeypatch.setattr(jira_fetch.urllib.request, "urlopen", fake_urlopen)

    env = {"JIRA_URL": "https://jira.example.invalid", "JIRA_PERSONAL_TOKEN": "token"}
    assert jira_fetch.get_issue("EAR-1", env) == issue
    assert jira_fetch.get_issue("EAR-1", env) == issue
    assert calls == {"rate": 1, "urlopen": 1}


def test_healthz_reports_queue_depth_warning(monkeypatch, tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from webapp import config
    from webapp.main import app

    db = tmp_path / "healthz.sqlite"
    engine = sqlalchemy.create_engine(f"sqlite:///{db.as_posix()}", future=True)
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(config, "platform_engine", lambda: engine)
    monkeypatch.setattr(config, "QUEUE_DEPTH_WARN", 2)
    monkeypatch.setattr(config, "strong_model_available", lambda: (True, "ok"))

    with sqlalchemy.orm.Session(engine) as session:
        repo = JobRunRepository(session)
        for idx in range(2):
            repo.start_run(
                legacy_job_id=f"job-queued-{idx}",
                type_="generate",
                product_key="wms",
                sprint="2026-06-11",
                label="Queued Job",
                owner_username="u1",
                lock_key="u1:wms",
                queue_name="generation",
                status="queued",
            )
        session.commit()

    payload = TestClient(app).get("/healthz").json()
    assert payload["queue_depth"] == {"generation": 2}
    assert payload["queue_depth_warn"] is True
    assert payload["queue_depth_threshold"] == 2


def test_worker_conninfo_and_pool_options_accept_sqlalchemy_psycopg_url(monkeypatch):
    monkeypatch.setenv("QA_WEBAPP_DATABASE_URL", "postgresql://u:p@db:5432/app")
    monkeypatch.setenv("QA_QUEUE_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("QA_QUEUE_POOL_MAX_SIZE", "2")
    monkeypatch.setenv("QA_QUEUE_POOL_MAX_LIFETIME", "3600")
    try:
        from worker.app import _psycopg_conninfo, connector_pool_options
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"worker dependencies unavailable: {type(exc).__name__}: {exc}")
    assert _psycopg_conninfo("postgresql+psycopg://u:p@db:5432/app") == "postgresql://u:p@db:5432/app"
    assert _psycopg_conninfo("postgresql://u:p@db:5432/app") == "postgresql://u:p@db:5432/app"
    assert connector_pool_options() == {"min_size": 1, "max_size": 2, "max_lifetime": 3600}


def test_worker_schema_skips_when_procrastinate_tables_exist(monkeypatch):
    monkeypatch.setenv("QA_WEBAPP_DATABASE_URL", "postgresql://u:p@db:5432/app")
    try:
        from worker import schema
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"worker dependencies unavailable: {type(exc).__name__}: {exc}")

    monkeypatch.setattr(schema, "_existing_tables", lambda: set(schema._REQUIRED_TABLES))

    class _Manager:
        def __init__(self, connector):
            self.connector = connector

        def apply_schema(self):
            raise AssertionError("schema should not be reapplied")

    monkeypatch.setattr(schema, "SchemaManager", _Manager)
    assert schema.ensure_schema() is False


def test_user_store_uses_platform_db_for_users_credentials_and_sessions(monkeypatch, tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    engine = sqlalchemy.create_engine("sqlite:///:memory:", future=True)
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(auth.config, "platform_engine", lambda: engine)
    monkeypatch.setattr(auth.config, "REVOKED_SESSIONS_FILE", tmp_path / "revoked_sessions.json")
    monkeypatch.setattr(auth, "_db_cipher", lambda: _FakeCipher())

    store = auth.UserStore()
    salt, pwd_hash = auth.hash_password("pw")
    store.upsert(auth.User(
        username="dbuser",
        display_name="DB User",
        role="admin",
        salt=salt,
        pwd_hash=pwd_hash,
        jira_pat="jira-db-secret",
        jira_url="https://jira.example.invalid",
        ai={"weak": {"provider": "openai", "base_url": "https://weak.example.invalid", "model": "weak-model", "api_key": "weak-db-secret"}},
    ))

    raw = json.dumps(engine.raw_connection().cursor().execute("select encrypted_value from user_credentials").fetchall(), default=str)
    assert "jira-db-secret" not in raw
    assert "weak-db-secret" not in raw

    identity = store.set_jira_identity("dbuser", "Verified Jira User", "https://jira.example.invalid/")
    raw_after_probe = json.dumps(engine.raw_connection().cursor().execute("select encrypted_value, metadata from user_credentials").fetchall(), default=str)
    assert identity["display_name"] == "Verified Jira User"
    assert "jira-db-secret" not in raw_after_probe
    assert "Verified Jira User" in raw_after_probe

    loaded = store.authenticate("dbuser", "pw")
    assert loaded is not None
    assert loaded.role == "admin"
    assert loaded.jira_pat == "jira-db-secret"
    assert loaded.jira_identity["display_name"] == "Verified Jira User"
    assert loaded.jira_identity["base_url"] == "https://jira.example.invalid"
    assert loaded.ai["weak"]["api_key"] == "weak-db-secret"

    token = auth.issue_session("dbuser", user_agent="pytest", ip_address="127.0.0.1")
    assert auth.read_session(token) == "dbuser"
    auth.revoke_session(token)
    assert auth.read_session(token) is None

    with sqlalchemy.orm.Session(engine) as session:
        db_user = session.scalar(select(models.User).where(models.User.username == "dbuser"))
        assert db_user is not None
        assert db_user.last_login_at is not None
        db_session = session.scalar(select(models.Session).where(models.Session.token_hash == auth._token_hash(token)))
        assert db_session is not None
        assert db_session.revoked_at is not None


def test_job_manager_reads_platform_db_runs_and_logs(monkeypatch):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from webapp.jobs import JobManager

    engine = sqlalchemy.create_engine("sqlite:///:memory:", future=True)
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(auth.config, "platform_engine", lambda: engine)

    with sqlalchemy.orm.Session(engine) as session:
        repo = JobRunRepository(session)
        repo.start_run(
            legacy_job_id="job-db-1",
            type_="generate",
            product_key="wms",
            sprint="2026-06-11",
            label="DB Job",
            owner_username="u1",
            lock_key="u1:wms",
            argv_display="python scripts/run_sprint.py --product wms",
            queue_name="generation",
            status="running",
        )
        repo.append_log("job-db-1", "line-1 token=super-secret")
        session.commit()

    manager = JobManager()
    job = manager.get("job-db-1", owner_username="u1")
    assert job is not None
    assert job.status == "running"
    assert job.lines == ["line-1 token=[REDACTED]"]
    assert manager.get("job-db-1", owner_username="u2") is None
    assert manager.log_entries_after("job-db-1", after_seq=0, owner_username="u1") == [
        {"seq": 1, "stream": "stdout", "line": "line-1 token=[REDACTED]"}
    ]
    assert manager.log_entries_after("job-db-1", after_seq=1, owner_username="u1") == []
    assert manager.is_busy("wms", "u1") == "job-db-1"
    assert manager.recent(5, "wms", "u1")[0].id == "job-db-1"


def test_job_routes_are_owner_scoped_and_sse_reads_db_logs(monkeypatch):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from webapp import config
    from webapp.main import app

    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
        future=True,
    )
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(config, "platform_engine", lambda: engine)
    monkeypatch.setattr(auth, "_db_cipher", lambda: _FakeCipher())

    salt, pwd_hash = auth.hash_password("pw")
    auth.store.upsert(auth.User(username="alice", display_name="Alice", salt=salt, pwd_hash=pwd_hash))
    auth.store.upsert(auth.User(username="bob", display_name="Bob", salt=salt, pwd_hash=pwd_hash))

    with sqlalchemy.orm.Session(engine) as session:
        repo = JobRunRepository(session)
        repo.start_run(
            legacy_job_id="job-route-1",
            type_="generate",
            product_key="wms",
            sprint="2026-06-11",
            label="DB Job",
            owner_username="alice",
            lock_key="alice:wms",
            queue_name="generation",
            status="done",
        )
        repo.append_log("job-route-1", "hello api_key=secret-value")
        repo.finish_run("job-route-1", status="done", rc=0)
        session.commit()

    client = TestClient(app)
    client.cookies.set("qa_session", auth.issue_session("alice"))
    status = client.get("/jobs/job-route-1")
    assert status.status_code == 200
    assert status.json()["lines"] == ["hello api_key=[REDACTED]"]
    with client.stream("GET", "/jobs/job-route-1/events") as response:
        body = response.read().decode("utf-8")
    assert response.status_code == 200
    assert "hello api_key=[REDACTED]" in body
    assert "secret-value" not in body
    assert "event: done" in body

    other = TestClient(app)
    other.cookies.set("qa_session", auth.issue_session("bob"))
    assert other.get("/jobs/job-route-1").status_code == 404


def test_worker_sweep_stalled_marks_old_db_runs(monkeypatch):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from worker import tasks

    engine = sqlalchemy.create_engine("sqlite:///:memory:", future=True)
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(tasks.config, "platform_engine", lambda: engine)

    with sqlalchemy.orm.Session(engine) as session:
        repo = JobRunRepository(session)
        run = repo.start_run(
            legacy_job_id="job-stalled",
            type_="generate",
            product_key="wms",
            sprint="2026-06-11",
            label="Old Job",
            owner_username="u1",
            lock_key="u1:wms",
            status="queued",
        )
        run.updated_at = datetime.utcnow() - timedelta(hours=3)
        session.commit()

    assert tasks.sweep_stalled(max_age_minutes=60)["updated"] == 1
    with sqlalchemy.orm.Session(engine) as session:
        run = session.scalar(select(models.PipelineRun).where(models.PipelineRun.legacy_job_id == "job-stalled"))
        assert run is not None
        assert run.status == "stalled"


def test_worker_db_artifact_mode_materializes_and_mirrors(monkeypatch, tmp_path):
    sqlalchemy = pytest.importorskip("sqlalchemy")
    from worker import tasks

    engine = sqlalchemy.create_engine("sqlite:///:memory:", future=True)
    models.Base.metadata.create_all(engine)
    monkeypatch.setattr(tasks.config, "platform_engine", lambda: engine)
    monkeypatch.setattr(tasks.config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(tasks.config, "USERDATA_DIR", tmp_path / "userdata")
    monkeypatch.setattr(tasks.config, "USE_DB_ARTIFACTS", True)

    try:
        with sqlalchemy.orm.Session(engine) as session:
            repo = ArtifactRepository(session)
            repo.upsert_artifact(
                product_key="wms",
                sprint="2026-06-11",
                ticket_key="EAR-1",
                owner_username="u1",
                name="questions.md",
                content_text="# Questions\n\n无\n",
            )
            session.commit()

        work_root, tickets_root = tasks._prepare_ticket_root("job-db-artifacts", "u1", "wms", "2026-06-11")
        assert work_root == tmp_path / ".work" / "job-db-artifacts"
        assert tickets_root == work_root / "tickets"
        materialized = tickets_root / "wms" / "2026-06-11" / "EAR-1" / "questions.md"
        assert materialized.read_text(encoding="utf-8") == "# Questions\n\n无\n"

        design = materialized.with_name("test-design.json")
        design.write_text("{\n  \"raw\": \"generated\"\n}\n", encoding="utf-8")
        tasks._mirror_after_run(work_root, "u1", "job-db-artifacts", "wms", "2026-06-11")

        cached_design = tmp_path / "userdata" / "u1" / "tickets" / "wms" / "2026-06-11" / "EAR-1" / "test-design.json"
        assert cached_design.read_text(encoding="utf-8") == "{\n  \"raw\": \"generated\"\n}\n"

        with sqlalchemy.orm.Session(engine) as session:
            repo = ArtifactRepository(session)
            rows = repo.list_artifacts(product_key="wms", sprint="2026-06-11", ticket_key="EAR-1", owner_username="u1")
            by_name = {row.name: row for row in rows}
            assert by_name["questions.md"].content_text == "# Questions\n\n无\n"
            assert by_name["test-design.json"].content_text == "{\n  \"raw\": \"generated\"\n}\n"
            assert by_name["test-design.json"].materialized_path == str(cached_design)
    finally:
        tasks.config.set_tickets_root(None)
