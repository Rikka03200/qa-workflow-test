"""Deployment configuration guardrails."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

from webapp import config

DEPLOY_DIR = config.REPO_ROOT / "deploy"
COMPOSE_FILE = DEPLOY_DIR / "compose.yaml"
ENV_EXAMPLE = DEPLOY_DIR / "env" / "local.env.example"
WORKER_GEN_EXAMPLE = DEPLOY_DIR / "env" / "worker-gen.example"
WORKER_REVIEW_EXAMPLE = DEPLOY_DIR / "env" / "worker-review.example"
RESTORE_DRILL = DEPLOY_DIR / "restore-drill.py"
WORKER_DRILL = DEPLOY_DIR / "worker-failure-drill.py"
SSE_LOAD_CHECK = DEPLOY_DIR / "sse-load-check.py"
REQUIREMENTS_WEB = config.REPO_ROOT / "webapp" / "requirements-web.txt"
REQUIREMENTS_TEST = config.REPO_ROOT / "webapp" / "requirements-test.txt"
UP_SH = DEPLOY_DIR / "up.sh"
UP_PS1 = DEPLOY_DIR / "up.ps1"

EXPECTED_LOCAL_ENV_KEYS = {
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "QA_WEBAPP_DATABASE_URL",
    "QA_WEBAPP_SECRET",
    "QA_FERNET_KEYS",
    "QA_WEBAPP_SECURE_COOKIE",
    "QA_WEBAPP_PORT",
    "QA_WEBAPP_CONCURRENCY",
    "QA_USE_WORKER",
    "QA_USE_DB_ARTIFACTS",
    "QA_KB_STRICT",
    "QA_DB_POOL_SIZE",
    "QA_DB_MAX_OVERFLOW",
    "QA_DB_POOL_RECYCLE",
    "QA_QUEUE_POOL_MIN_SIZE",
    "QA_QUEUE_POOL_MAX_SIZE",
    "QA_QUEUE_POOL_MAX_LIFETIME",
    "QA_GENERATION_WORKER_CONCURRENCY",
    "QA_REVIEW_WORKER_CONCURRENCY",
    "QA_JIRA_RATE_QPS",
    "QA_JIRA_RATE_BURST",
    "QA_JIRA_CACHE_TTL_SECONDS",
    "QA_QUEUE_DEPTH_WARN",
}


def _env_keys(text: str) -> set[str]:
    keys = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0])
    return keys


def _command(service: dict) -> list[str]:
    command = service.get("command") or []
    return command if isinstance(command, list) else [command]


def test_compose_splits_generation_and_review_workers():
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = compose["services"]

    assert {"postgres", "migrate", "kb-migrate", "queue-migrate", "web", "worker-gen", "worker-review"} <= set(services)
    assert services["worker-gen"].get("profiles") == ["worker"]
    assert services["worker-review"].get("profiles") == ["worker"]
    assert services["worker"].get("profiles") == ["legacy-worker"]

    gen_command = _command(services["worker-gen"])
    review_command = _command(services["worker-review"])
    kb_command = _command(services["kb-migrate"])

    assert kb_command == ["python", "scripts/kb_store.py", "migrate"]
    assert services["kb-migrate"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert services["web"]["depends_on"]["kb-migrate"]["condition"] == "service_completed_successfully"
    assert services["queue-migrate"]["depends_on"]["kb-migrate"]["condition"] == "service_completed_successfully"
    assert "generation" in gen_command
    assert "review,maintenance" in review_command
    assert "--concurrency" in gen_command
    assert "${QA_GENERATION_WORKER_CONCURRENCY:-3}" in gen_command
    assert "--concurrency" in review_command
    assert "${QA_REVIEW_WORKER_CONCURRENCY:-6}" in review_command


def test_compose_keeps_deployment_health_and_backup_profiles():
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = compose["services"]

    assert "healthcheck" in services["web"]
    assert "healthcheck" in services["worker-gen"]
    assert "healthcheck" in services["worker-review"]
    assert services["backup"].get("profiles") == ["backup"]
    assert "backups" in compose["volumes"]


def test_docker_defaults_use_db_artifact_mode():
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    app_env = compose["x-app-env"]
    assert app_env["QA_USE_DB_ARTIFACTS"] == "${QA_USE_DB_ARTIFACTS:-1}"
    assert app_env["QA_KB_STRICT"] == "${QA_KB_STRICT:-1}"

    requirements = REQUIREMENTS_WEB.read_text(encoding="utf-8")
    assert "claude-agent-sdk>=" in requirements

    module_path = DEPLOY_DIR / "init-local-env.py"
    spec = importlib.util.spec_from_file_location("init_local_env", module_path)
    assert spec and spec.loader
    init_local_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_local_env)

    generated = init_local_env._render()
    assert "QA_USE_DB_ARTIFACTS=1" in generated
    assert "QA_KB_STRICT=1" in generated
    assert "QA_USE_DB_ARTIFACTS=1" in ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "QA_KB_STRICT=1" in ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "QA_USE_DB_ARTIFACTS=1" in WORKER_GEN_EXAMPLE.read_text(encoding="utf-8")
    assert "QA_KB_STRICT=1" in WORKER_GEN_EXAMPLE.read_text(encoding="utf-8")
    assert "QA_USE_DB_ARTIFACTS=1" in WORKER_REVIEW_EXAMPLE.read_text(encoding="utf-8")
    assert "QA_KB_STRICT=1" in WORKER_REVIEW_EXAMPLE.read_text(encoding="utf-8")


def test_test_requirements_include_runtime_and_pytest():
    requirements = REQUIREMENTS_TEST.read_text(encoding="utf-8")

    assert "-r requirements-web.txt" in requirements
    assert "pytest>=" in requirements


def test_one_click_deploy_wrappers_use_worker_compose_profile():
    sh = UP_SH.read_text(encoding="utf-8")
    ps1 = UP_PS1.read_text(encoding="utf-8")

    assert "python init-local-env.py" in sh
    assert "docker compose --env-file env/local.env --profile worker up -d --build" in sh
    assert "kb-migrate" in sh
    assert "python init-local-env.py" in ps1
    assert "docker compose --env-file env/local.env --profile worker up -d --build" in ps1
    assert "kb-migrate" in ps1


def test_local_env_example_matches_generated_nonsecret_keys():
    module_path = DEPLOY_DIR / "init-local-env.py"
    spec = importlib.util.spec_from_file_location("init_local_env", module_path)
    assert spec and spec.loader
    init_local_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_local_env)

    generated = init_local_env._render()
    example = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert EXPECTED_LOCAL_ENV_KEYS <= _env_keys(generated)
    assert EXPECTED_LOCAL_ENV_KEYS <= _env_keys(example)


def test_deploy_examples_only_contain_placeholders():
    examples = list((DEPLOY_DIR / "env").glob("*.example")) + list((DEPLOY_DIR / "secrets").glob("*.example"))
    assert examples
    forbidden = re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{16,})")
    for path in examples:
        text = path.read_text(encoding="utf-8")
        assert "REPLACE_ME" in text or path.name.endswith(".example")
        assert not forbidden.search(text), path


def test_operational_drill_scripts_are_safe_by_default():
    restore = RESTORE_DRILL.read_text(encoding="utf-8")
    worker = WORKER_DRILL.read_text(encoding="utf-8")
    sse = SSE_LOAD_CHECK.read_text(encoding="utf-8")

    assert "TEMP_DB_PREFIX = \"qa_restore_drill_\"" in restore
    assert "--drop-temp-db-after" in restore
    assert "refusing to operate on non-drill database name" in restore
    assert "REQUIRED_TABLES" in restore

    assert "--execute" in worker
    assert "status\"] = \"planned\"" in worker
    assert "choices=sorted(ALLOWED_WORKERS)" in worker

    assert "QA_LOAD_USERNAME" in sse
    assert "QA_LOAD_PASSWORD" in sse
    assert "--connections" in sse
    report_body = sse.split("def write_reports", 1)[1].split("def main", 1)[0]
    assert "Cookie" not in report_body


def test_operational_reports_are_ignored_by_git_and_docker_context():
    gitignore = (config.REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (config.REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "deploy/reports/" in gitignore
    assert "deploy/reports" in dockerignore
