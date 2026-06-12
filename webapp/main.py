"""FastAPI 应用装配。

启动：
  pip install -r webapp/requirements-web.txt
  python -m webapp.auth adduser <你>            # 建第一个账号
  python -m webapp.main                          # 或 uvicorn webapp.main:app --port 8800
内网部署：绑内网 host + 反向代理（Nginx/Caddy）做 TLS；设 QA_WEBAPP_SECURE_COOKIE=1。
"""

from __future__ import annotations

import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .deps import Forbidden, NotAuthenticated
from . import auth
from .routers import admin, jobs as jobs_router
from .routers import pages, ticket_actions


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Best-effort KB schema initialization; Markdown fallback remains available."""
    config._ensure_data_dir()
    try:
        from webapp.services import scripts_loader
        store = scripts_loader.load_normal("kb_store")
        store.init_schema()
    except Exception as exc:  # noqa: BLE001
        print(f"WARN: KB schema init skipped: {type(exc).__name__}: {exc}")
    yield


app = FastAPI(title="QA 控制台 · qa-workflow v3", docs_url=None, redoc_url=None, lifespan=lifespan)

config._ensure_data_dir()
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


class _UserRootMiddleware:
    """纯 ASGI 中间件：按会话 cookie 解析当前用户，设请求级工单根（contextvar）。
    用纯 ASGI（非 BaseHTTPMiddleware）确保 contextvar 能正确传到下游（含 threadpool 同步路由）。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            username = None
            try:
                from http.cookies import SimpleCookie
                raw = next((v for k, v in (scope.get("headers") or []) if k == b"cookie"), b"")
                if raw:
                    morsel = SimpleCookie(raw.decode("latin-1")).get(config.SESSION_COOKIE)
                    username = auth.read_session(morsel.value) if morsel else None
            except Exception:  # noqa: BLE001
                username = None
            config.set_user_root(username)
        await self.app(scope, receive, send)


class _CSRFMiddleware:
    """Double-submit CSRF protection for browser write requests."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        from http.cookies import SimpleCookie
        method = (scope.get("method") or "GET").upper()
        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        raw_cookie = headers.get(b"cookie", b"")
        cookie = SimpleCookie(raw_cookie.decode("latin-1")) if raw_cookie else SimpleCookie()
        csrf_cookie = cookie.get(config.CSRF_COOKIE)
        cookie_token = csrf_cookie.value if csrf_cookie else ""

        if method not in self.SAFE_METHODS:
            body = b""
            more_body = True
            while more_body:
                message = await receive()
                if message.get("type") != "http.request":
                    break
                body += message.get("body", b"")
                more_body = bool(message.get("more_body"))

            header_token = headers.get(b"x-csrf-token", b"").decode("latin-1")
            form_token = ""
            content_type = headers.get(b"content-type", b"").decode("latin-1")
            if body and "application/x-www-form-urlencoded" in content_type:
                from urllib.parse import parse_qs
                form_token = (parse_qs(body.decode("utf-8", "ignore")).get("csrf_token") or [""])[0]
            if not auth.verify_csrf_token(cookie_token, header_token or form_token):
                response = JSONResponse({"ok": False, "error": "CSRF token invalid"}, status_code=403)
                await response(scope, receive, send)
                return

            sent = False

            async def replay_receive():
                nonlocal sent
                if sent:
                    return {"type": "http.request", "body": b"", "more_body": False}
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            await self.app(scope, replay_receive, send)
            return

        need_cookie = not cookie_token
        new_token = auth.issue_csrf_token() if need_cookie else cookie_token

        async def send_with_csrf(message):
            if message.get("type") == "http.response.start" and need_cookie:
                headers_list = list(message.get("headers") or [])
                secure = "; Secure" if os.environ.get("QA_WEBAPP_SECURE_COOKIE") else ""
                headers_list.append((
                    b"set-cookie",
                    f"{config.CSRF_COOKIE}={new_token}; Path=/; Max-Age={config.SESSION_MAX_AGE}; SameSite=Lax{secure}".encode("latin-1"),
                ))
                message = {**message, "headers": headers_list}
            await send(message)

        await self.app(scope, receive, send_with_csrf)


app.add_middleware(_UserRootMiddleware)
app.add_middleware(_CSRFMiddleware)

app.include_router(admin.router)
app.include_router(pages.router)
app.include_router(ticket_actions.router)
app.include_router(jobs_router.router)


@app.exception_handler(NotAuthenticated)
async def _auth_redirect(request: Request, exc: NotAuthenticated):
    """未登录：HTMX/API 返回 401（前端跳转），普通页面 303 跳 /login。"""
    if request.headers.get("HX-Request") == "true":
        return RedirectResponse("/login", status_code=401, headers={"HX-Redirect": "/login"})
    nxt = request.url.path
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    if request.headers.get("HX-Request") == "true":
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)


@app.get("/healthz")
async def healthz():
    ok, reason = config.strong_model_available()
    kb_ok, kb_reason = _kb_store_available()
    platform_ok, platform_reason = config.platform_db_available()
    queue_depth, queue_reason = _queue_depths()
    queue_depth_warn = any(depth >= config.QUEUE_DEPTH_WARN for depth in queue_depth.values()) if queue_depth else False
    return {"ok": True, "strong_model": ok, "strong_reason": reason,
            "kb_db": kb_ok, "kb_reason": kb_reason,
            "platform_db": platform_ok, "platform_reason": platform_reason,
            "queue_depth": queue_depth, "queue_reason": queue_reason,
            "queue_depth_warn": queue_depth_warn, "queue_depth_threshold": config.QUEUE_DEPTH_WARN,
            "users": auth.store.count()}


def _kb_store_available() -> tuple[bool, str]:
    try:
        from webapp.services import scripts_loader
        store = scripts_loader.load_normal("kb_store")
        return store.available()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _queue_depths() -> tuple[dict[str, int], str]:
    engine = config.platform_engine()
    if engine is None:
        return {}, "platform DB not configured"
    try:
        from core.store.db import session_scope
        from core.store.repositories import JobRunRepository
        with session_scope(engine) as session:
            depths = JobRunRepository(session).queue_depths()
        return depths, "ok"
    except Exception as exc:  # noqa: BLE001
        return {}, f"{type(exc).__name__}: {exc}"


def main() -> None:
    import uvicorn
    uvicorn.run("webapp.main:app", host=config.HOST, port=config.PORT,
                reload=bool(os.environ.get("QA_WEBAPP_RELOAD")))


if __name__ == "__main__":
    main()
