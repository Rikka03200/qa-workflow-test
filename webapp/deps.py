"""共享依赖与工具：鉴权依赖、invoke() 错误隔离、按用户 env、审计、模板。"""

from __future__ import annotations

import html
import os
import re
from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import auth, config
from .services import scripts_loader


# ----------------------------- 鉴权依赖 -----------------------------

class NotAuthenticated(Exception):
    """未登录。由 main 的异常处理器转成 /login 跳转（页面）或 401（HTMX/API）。"""


class Forbidden(Exception):
    """已登录但无权访问。由 main 转成 403。"""


def _role_key(role: str) -> str:
    normalized = (role or "").strip().lower()
    if normalized in {"admin", "管理员"}:
        return "admin"
    if normalized in {"viewer", "只读", "只读用户"}:
        return "viewer"
    return "qa"


def require_role(*roles: str):
    allowed = {_role_key(role) for role in roles}

    def _dep(request: Request) -> auth.User:
        user = require_user(request)
        if _role_key(user.role) not in allowed:
            raise Forbidden()
        return user

    return _dep


def current_user_optional(request: Request) -> Optional[auth.User]:
    token = request.cookies.get(config.SESSION_COOKIE, "")
    username = auth.read_session(token)
    if not username:
        return None
    return auth.store.get(username)


def require_user(request: Request) -> auth.User:
    user = current_user_optional(request)
    if user is None:
        raise NotAuthenticated()
    return user


# ----------------------------- 错误隔离 -----------------------------

def invoke(fn: Callable, *args: Any, **kwargs: Any) -> dict:
    """所有 in-process 调用现有脚本都过这层。

    现有脚本用 SystemExit 当错误类型（jira_fetch/_load_env/select_sprint 多处），
    用 RuntimeError 表示 questions 闸门失败——必须隔离，否则拖垮 worker / 整个进程。
    返回 {"ok": bool, "data"|"error", "kind"}。
    """
    try:
        return {"ok": True, "data": fn(*args, **kwargs)}
    except SystemExit as e:
        return {"ok": False, "error": str(e) or "脚本以 SystemExit 退出", "kind": "system_exit"}
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "kind": "gate"}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e), "kind": "not_found"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "kind": "exc"}


# ----------------------------- 按用户凭证 -----------------------------

def user_env(user: Optional[auth.User]) -> dict[str, str]:
    """in-process 只读 Jira 用的 env：服务账号配置 + 覆盖为该用户自己的 Jira PAT。

    用户已要求「各用各的 Jira PAT」。未设置 PAT 的用户回退到 config 的服务账号，
    保证开箱可用。
    """
    try:
        env = dict(scripts_loader.load_env().parse_config())
    except SystemExit:
        env = {}
    except Exception:
        env = {}
    if user and user.jira_pat:
        env["JIRA_PERSONAL_TOKEN"] = user.jira_pat
        env.pop("JIRA_API_TOKEN", None)  # 用 PAT 时清掉 basic，避免双凭证歧义
    if user and getattr(user, "jira_url", ""):
        env["JIRA_URL"] = user.jira_url
    return env


def _global_cheap_provider() -> str:
    """全局（config.local.yaml）弱模型协议，缺省 anthropic。"""
    cfg = config.raw_config()
    a = (cfg.get("ai") or {}).get("cheap_provider") or cfg.get("cheap_provider") or {}
    return (a.get("provider") or "anthropic")


_BASE_SUBPROCESS_ENV = (
    "PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "LANG", "LC_ALL", "TMP", "TEMP",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "QA_JIRA_RATE_QPS", "QA_JIRA_RATE_BURST", "QA_JIRA_CACHE_TTL_SECONDS", "QA_JIRA_CACHE_DISABLE",
)
_ALLOWED_CONFIG_ENV = {
    "JIRA_URL", "JIRA_PERSONAL_TOKEN", "JIRA_USERNAME", "JIRA_API_TOKEN", "JIRA_SSL_VERIFY",
    "CONFLUENCE_URL", "CONFLUENCE_PERSONAL_TOKEN", "CONFLUENCE_USERNAME", "CONFLUENCE_API_TOKEN", "CONFLUENCE_SSL_VERIFY",
    "CHEAP_MODEL_BASE_URL", "CHEAP_MODEL_API_KEY", "CHEAP_MODEL_NAME", "CHEAP_MODEL_SMALL_NAME",
    "CHEAP_MODEL_PROVIDER", "CHEAP_MODEL_MAX_TOKENS", "CHEAP_MODEL_ENABLED", "READ_ONLY_MODE",
    "QA_JIRA_RATE_QPS", "QA_JIRA_RATE_BURST", "QA_JIRA_CACHE_TTL_SECONDS", "QA_JIRA_CACHE_DISABLE",
}


def _clean_optional_env(value: str | None) -> str:
    return str(value).strip() if value else ""


def subprocess_env(user: Optional[auth.User]) -> dict[str, str]:
    """弱链子进程 env：从零构造白名单，避免平台/强模型/SSO 密钥泄露。"""
    env = {k: v for k, v in os.environ.items() if k in _BASE_SUBPROCESS_ENV and v}
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    })
    try:
        base = scripts_loader.load_env().parse_config()
    except SystemExit:
        base = {}
    except Exception:
        base = {}
    for key, value in base.items():
        if key in _ALLOWED_CONFIG_ENV and value:
            env[key] = value
    if user:
        env["QA_TICKETS_ROOT"] = str(config.user_tickets_dir(user.username))
        if user.display_name:
            env["QA_SELECT_TESTER"] = user.display_name
        if user.jira_pat:
            env["JIRA_PERSONAL_TOKEN"] = user.jira_pat
            env.pop("JIRA_API_TOKEN", None)
        if getattr(user, "jira_url", ""):
            env["JIRA_URL"] = user.jira_url
    w = (user.ai or {}).get("weak") if user else None
    if w and _clean_optional_env(w.get("base_url")) and _clean_optional_env(w.get("api_key")):
        env["CHEAP_MODEL_BASE_URL"] = _clean_optional_env(w.get("base_url")).rstrip("/")
        env["CHEAP_MODEL_API_KEY"] = _clean_optional_env(w.get("api_key"))
        if _clean_optional_env(w.get("model")):
            env["CHEAP_MODEL_NAME"] = _clean_optional_env(w.get("model"))
        env["CHEAP_MODEL_PROVIDER"] = _clean_optional_env(w.get("provider")) or "anthropic"
        env["CHEAP_MODEL_ENABLED"] = "true"
    elif w:
        env.pop("CHEAP_MODEL_API_KEY", None)
        env.pop("CHEAP_MODEL_ENABLED", None)
        provider = _clean_optional_env(w.get("provider")) or "anthropic"
        if provider != _global_cheap_provider():
            env["CHEAP_MODEL_PROVIDER"] = provider
            if _clean_optional_env(w.get("base_url")):
                env["CHEAP_MODEL_BASE_URL"] = _clean_optional_env(w.get("base_url")).rstrip("/")
            else:
                env.pop("CHEAP_MODEL_BASE_URL", None)
            if _clean_optional_env(w.get("model")):
                env["CHEAP_MODEL_NAME"] = _clean_optional_env(w.get("model"))
    return env


def user_anthropic_endpoint(user: Optional[auth.User]) -> dict:
    """该用户生效的强模型(复核)端点：用户自配的 ai.strong 覆盖全局 config，逐字段回退。

    带 provider（anthropic|openai）。安全约束：当用户选的协议与全局不同（如全局 anthropic、
    用户切 openai），凭证【不跨协议回退】——必须用用户自己的 base_url/api_key，避免把
    anthropic 的 key 误发给 openai 端点。
    """
    glob = config.anthropic_endpoint()
    s = (user.ai or {}).get("strong") if user else None
    if not s:
        return glob
    prov = s.get("provider") or glob.get("provider") or "anthropic"
    if prov != (glob.get("provider") or "anthropic"):
        return {"base_url": s.get("base_url"), "api_key": s.get("api_key"),
                "model": s.get("model"), "provider": prov}
    return {
        "base_url": s.get("base_url") or glob.get("base_url"),
        "api_key": s.get("api_key") or glob.get("api_key"),
        "model": s.get("model") or glob.get("model"),
        "provider": prov,
    }


# ----------------------------- 审计 -----------------------------

_SECRET_ASSIGN_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|pat|personal[_-]?token)(\s*[=:]\s*)([^\s&;,]+)")
_URL_PASSWORD_RE = re.compile(r"(://[^:/\s]+:)([^@\s]+)(@)")


def redact_secret(text: str) -> str:
    """Redact common secret shapes before writing UI/DB/file logs."""
    value = str(text or "")
    value = _SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)
    value = _URL_PASSWORD_RE.sub(r"\1[REDACTED]\3", value)
    return value


def audit(user: Optional[auth.User], action: str, detail: str = "") -> None:
    """操作者留痕：登录、触发生成/审计、保存答案、保存 JSON 都记一行。"""
    config._ensure_data_dir()
    who = user.username if user else "-"
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t{who}\t{action}\t{redact_secret(detail)}\n"
    try:
        with config.AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ----------------------------- 模板 -----------------------------

templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))
templates.env.globals["product_display"] = config.product_display
templates.env.globals["csrf_cookie_name"] = config.CSRF_COOKIE


def render(name: str, ctx: dict, **kwargs):
    """版本无关的模板渲染。新版 Starlette 要求 TemplateResponse(request, name, context)；
    本包装器统一从 ctx 取 request，调用方仍写 render(name, {...,'request':request})。
    **kwargs 透传（如 status_code）。"""
    return templates.TemplateResponse(ctx.get("request"), name, ctx, **kwargs)


def _escape_pre(text: str) -> str:
    """把 markdown/文本产物安全转义后供 <pre> 展示（零依赖、防注入）。"""
    return html.escape(text or "")


templates.env.filters["pre"] = _escape_pre


def _br_safe(text):
    """用例树节点文本展示：先转义所有 HTML，再仅把 <br> 变体还原成真正换行。
    test-design.json 里保留 `<br>`（CodeArts 用），界面不显示字面量 `<br>`，且不放过其它 HTML（防注入）。"""
    import re as _re
    from markupsafe import Markup, escape as _esc
    if not text:
        return ""
    s = _re.sub(r"&lt;br\s*/?&gt;", "<br>", str(_esc(str(text))), flags=_re.I)
    return Markup(s)


templates.env.filters["br_safe"] = _br_safe

from .services import humanize as _humanize  # noqa: E402
templates.env.filters["humanize"] = _humanize.humanize
templates.env.filters["evidence"] = _humanize.evidence
