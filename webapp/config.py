"""webapp 配置中枢：路径、签名密钥、会话、特性开关、强模型端点。

原则：
- 仓库工作树（REPO_ROOT）= webapp/ 的父目录，是唯一数据真源。
- 密钥（签名 cookie 用）优先取环境变量 QA_WEBAPP_SECRET；否则在 data/secret.key
  持久化一份随机密钥（gitignored），重启后会话不失效。
- 强模型（Agent SDK）是否可用 = SDK 已安装 ∧ config.local.yaml 配好 ANTHROPIC 端点；
  二者缺一即降级为「复制命令贴回 Claude Code」。
"""

from __future__ import annotations

import contextvars
import os
import re
import secrets
from functools import lru_cache
from pathlib import Path

from core.productcfg import DEFAULT_PRODUCT

# ---- 路径 ----
WEBAPP_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEBAPP_DIR.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
TICKETS_DIR = REPO_ROOT / "tickets"
STATIC_DIR = WEBAPP_DIR / "static"
TEMPLATES_DIR = WEBAPP_DIR / "templates"
DATA_DIR = WEBAPP_DIR / "data"
USERS_FILE = DATA_DIR / "users.json"
AUDIT_LOG = DATA_DIR / "audit.log"
FERNET_KEY_FILE = DATA_DIR / "fernet.key"

# 每用户独立工单根：userdata/<用户名>/tickets（知识库 _kb 仍在 REPO_ROOT 共享）。
USERDATA_DIR = REPO_ROOT / "userdata"
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_user_tickets_root: contextvars.ContextVar = contextvars.ContextVar("user_tickets_root", default=None)


def valid_username(name: str) -> bool:
    return bool(name and _USERNAME_RE.match(name))


def user_tickets_dir(username: str) -> Path:
    """该用户独立的工单根 userdata/<用户名>/tickets；用户名非法→指向必不存在的隔离段。"""
    safe = username if valid_username(username) else "__invalid__"
    return USERDATA_DIR / safe / "tickets"


def set_user_root(username) -> None:
    """请求级设置当前用户工单根（由 ASGI 中间件按会话调用）；空则清空（回退 legacy）。"""
    _user_tickets_root.set(user_tickets_dir(username) if username else None)


def set_tickets_root(root: Path | None) -> None:
    """Set the current context ticket root for worker/materialized execution."""
    _user_tickets_root.set(root)


def tickets_root() -> Path:
    """当前生效工单根：优先请求级用户根，否则 legacy TICKETS_DIR（CLI / 无会话兜底）。"""
    return _user_tickets_root.get() or TICKETS_DIR

# ---- 会话 ----
SESSION_COOKIE = "qa_session"
CSRF_COOKIE = "qa_csrf"
SESSION_MAX_AGE = 60 * 60 * 12  # 12 小时
REVOKED_SESSIONS_FILE = DATA_DIR / "revoked_sessions.json"

# ---- 服务绑定（默认仅本机；内网部署由反向代理做 TLS） ----
HOST = os.environ.get("QA_WEBAPP_HOST", "127.0.0.1")
PORT = int(os.environ.get("QA_WEBAPP_PORT", "8800"))

# 弱链子进程默认并发（透传 run_sprint.py --concurrency）
DEFAULT_CONCURRENCY = int(os.environ.get("QA_WEBAPP_CONCURRENCY", "4"))

# 使用 Procrastinate worker 执行作业；未开启时保留 Web 线程执行，便于本地开发与回退。
USE_WORKER = os.environ.get("QA_USE_WORKER", "").lower() in {"1", "true", "yes", "on"}

# P3a DB artifact mode：worker 先物化到 .work/<run>/tickets，再把白名单产物回灌 DB。
USE_DB_ARTIFACTS = os.environ.get("QA_USE_DB_ARTIFACTS", "").lower() in {"1", "true", "yes", "on"}

# 队列深度告警阈值；/healthz 暴露水位，生产监控可据此告警。
QUEUE_DEPTH_WARN = int(os.environ.get("QA_QUEUE_DEPTH_WARN", "50") or "50")

# 产品线展示短名（webapp 用；覆盖 config.local.yaml 里偏长的 display_name）。
# WMS 含 web/仓配App/采配App/POS/TMS 等多端，统一就叫「WMS」。
PRODUCT_DISPLAY = {DEFAULT_PRODUCT: "WMS"}

# 新用户默认 AI 配置（仅 provider/base_url/model 作预填；API Key 必须用户自填）。
AI_DEFAULTS = {
    "weak": {"provider": "anthropic",
             "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
             "model": "qwen3.7-max"},
    "strong": {"provider": "openai",
               "base_url": "https://new-api.nhsoftcloud.com/v1",
               "model": "gpt-5.5"},
}


def jira_default_url() -> str:
    """Jira 默认地址（来自 config.local.yaml；用户可在设置里覆盖）。"""
    u = (raw_config().get("jira") or {}).get("url")
    return u.rstrip("/") if (u and u != "REPLACE_ME") else ""


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def secret_key() -> bytes:
    """签名 cookie 的密钥。env 优先，否则持久化随机密钥到 data/secret.key。"""
    env = os.environ.get("QA_WEBAPP_SECRET")
    if env:
        return env.encode("utf-8")
    _ensure_data_dir()
    key_file = DATA_DIR / "secret.key"
    if key_file.exists():
        data = key_file.read_bytes().strip()
        if data:
            return data
    key = secrets.token_hex(32).encode("utf-8")
    # 0600 在 Windows 上不强制，但写入 gitignored data/ 已足够
    key_file.write_bytes(key)
    return key


def raw_config() -> dict:
    """读 config.local.yaml 的嵌套 dict（复用 scripts/_load_env），失败返回 {}。"""
    if not (REPO_ROOT / "config" / "config.local.yaml").exists():
        return {}
    try:
        from .services import scripts_loader  # 延迟导入避免循环
        _load_env = scripts_loader.load_normal("_load_env")
        return _load_env.load_raw_config() or {}
    except SystemExit:
        # config.local.yaml 不存在时 _load_env 会 sys.exit；前端容忍并提示去配置页
        return {}
    except Exception:
        return {}


def products() -> dict[str, dict]:
    """返回 {product: {display_name, ...}}，来自 config.local.yaml 的 products 段。"""
    cfg = raw_config()
    out = dict((cfg.get("products") or {}))
    if not out:  # 配置缺失时至少给个默认产品兜底，让看板能渲染空态
        out = {DEFAULT_PRODUCT: {"display_name": PRODUCT_DISPLAY[DEFAULT_PRODUCT]}}
    return out


@lru_cache(maxsize=1)
def platform_engine():
    """平台数据库引擎；未配置时返回 None，保持文件模式可运行。"""
    try:
        from core.store.db import engine_from_url
        return engine_from_url()
    except Exception:
        return None


def platform_db_available() -> tuple[bool, str]:
    engine = platform_engine()
    if engine is None:
        return False, "未配置 QA_WEBAPP_DATABASE_URL"
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def product_display(product: str) -> str:
    if product in PRODUCT_DISPLAY:           # webapp 短名优先
        return PRODUCT_DISPLAY[product]
    p = products().get(product) or {}
    return p.get("display_name") or product.upper()


_PLACEHOLDERS = ("REPLACE_ME", "REPLACE_ME_OR_LEAVE_BLANK", "")


def _real(v):
    """占位符/空 → None；否则原值。"""
    return v if (v and str(v).strip() not in _PLACEHOLDERS) else None


def anthropic_endpoint() -> dict:
    """强模型（Agent SDK）端点配置：ai.anthropic.{base_url,api_key,default_model}。

    用户使用兼容 ANTHROPIC API 的第三方端点，需自行在 config.local.yaml 配置（或在设置页按用户配）。
    返回 {base_url, model, api_key}；占位符/空一律归一为 None（不会把假值传给 SDK）。
    """
    a = ((raw_config().get("ai") or {}).get("anthropic")) or {}
    return {
        "base_url": _real(a.get("base_url")) or _real(os.environ.get("ANTHROPIC_BASE_URL")),
        "api_key": _real(a.get("api_key")) or _real(os.environ.get("ANTHROPIC_API_KEY")),
        "model": _real(a.get("default_model")) or _real(os.environ.get("ANTHROPIC_MODEL")),
        "provider": (_real(a.get("provider")) or "anthropic"),
    }


def strong_model_available() -> tuple[bool, str]:
    """(可用?, 原因)。委托 runner.availability_for 做单一真源（provider 感知）。"""
    from .strong import runner  # 延迟导入避免循环
    return runner.availability_for(anthropic_endpoint())
