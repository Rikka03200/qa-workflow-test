"""页面路由：登录/登出、首页、sprint 看板、工单详情、配置。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, config
from ..deps import (audit, current_user_optional, render, require_user, templates,
                    user_anthropic_endpoint, user_env)
from ..jobs import manager
from ..services import digest
from ..services import questions as q_svc
from ..services import scripts_loader, selection, tickets, tree
from ..strong import runner as strong_runner
from core.productcfg import DEFAULT_PRODUCT

router = APIRouter()
_SECURE_COOKIE = bool(os.environ.get("QA_WEBAPP_SECURE_COOKIE"))


def _set_session(resp, request: Request, username: str) -> None:
    client_ip = request.client.host if request.client else ""
    token = auth.issue_session(username, user_agent=request.headers.get("user-agent", ""), ip_address=client_ip)
    resp.set_cookie(config.SESSION_COOKIE, token,
                    max_age=config.SESSION_MAX_AGE, httponly=True,
                    samesite="lax", secure=_SECURE_COOKIE)
    resp.set_cookie(config.CSRF_COOKIE, auth.issue_csrf_token(),
                    max_age=config.SESSION_MAX_AGE, httponly=False,
                    samesite="lax", secure=_SECURE_COOKIE)


def _nav(active: str, product: str) -> dict:
    return {"active": active, "product": product,
            "product_display": config.product_display(product),
            "products": config.products()}


def _safe_next(nxt: str) -> str:
    """只允许站内相对跳转，防开放重定向（//evil、http://evil 等一律回退到 /）。"""
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and not nxt.startswith("/\\"):
        return nxt
    return "/"


# ----------------------------- 鉴权 -----------------------------

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if current_user_optional(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return render("login.html", {"request": request, "next": _safe_next(next), "error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...),
                 password: str = Form(...), next: str = Form("/")):
    user = auth.store.authenticate(username.strip(), password)
    if not user:
        audit(None, "login.fail", username)
        return render(
            "login.html", {"request": request, "next": _safe_next(next), "error": "用户名或密码错误"},
            status_code=401)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    _set_session(resp, request, user.username)
    audit(user, "login.ok")
    return resp


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request, error: str = ""):
    return RedirectResponse("/login", status_code=303)


@router.post("/register", response_class=HTMLResponse)
def register_submit(request: Request, username: str = Form(...), password: str = Form(...),
                    password2: str = Form(""), display_name: str = Form("")):
    return render("login.html", {
        "request": request, "next": "/", "error": "已关闭开放注册，请联系管理员创建账号"
    }, status_code=403)


@router.get("/logout")
def logout(request: Request):
    user = current_user_optional(request)
    token = request.cookies.get(config.SESSION_COOKIE, "")
    auth.revoke_session(token)
    audit(user, "logout")
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(config.SESSION_COOKIE)
    resp.delete_cookie(config.CSRF_COOKIE)
    return resp


# ----------------------------- 首页 -----------------------------

@router.get("/", response_class=HTMLResponse)
def index(request: Request, product: str = DEFAULT_PRODUCT, user=Depends(require_user)):
    prods = config.products()
    if product not in prods:
        product = next(iter(prods), DEFAULT_PRODUCT)
    ov = selection.overview(product, user.username)
    ok, reason = config.strong_model_available()
    my_jobs = [j.public() for j in manager.recent(8, product, user.username)]
    return render("index.html", {
        "request": request, "user": user.public(), "nav": _nav("index", product),
        "overview": ov, "recent_jobs": my_jobs,
        "busy": manager.is_busy(product, user.username), "strong_ok": ok, "strong_reason": reason,
    })


# ----------------------------- sprint 看板 -----------------------------

@router.get("/sprint/{product}/{date}", response_class=HTMLResponse)
def sprint_board(request: Request, product: str, date: str, user=Depends(require_user)):
    if not (tickets.safe_product(product) and tickets.safe_date(date)):
        return RedirectResponse("/", status_code=303)
    bd = selection.board(product, date)
    ok, reason = config.strong_model_available()
    my_jobs = [j.public() for j in manager.recent(5, product, user.username)]
    return render("sprint.html", {
        "request": request, "user": user.public(), "nav": _nav("sprint", product),
        "board": bd, "date": date, "sprint_dates": selection.sprint_dates(product),
        "recent_jobs": my_jobs,
        "busy": manager.is_busy(product, user.username), "strong_ok": ok, "strong_reason": reason,
    })


@router.get("/sprint/{product}/{date}/kb", response_class=HTMLResponse)
def sprint_kb(request: Request, product: str, date: str, user=Depends(require_user)):
    """看板级知识回填：聚合本期各工单的入库提案，支持批量提炼 + 跨工单勾选批量入库。"""
    if not (tickets.safe_product(product) and tickets.safe_date(date)):
        return RedirectResponse("/", status_code=303)
    from ..strong import kb_extract
    bd = selection.board(product, date)
    items = []
    for row in bd.get("rows", []):
        if not row.get("is_run"):
            continue
        td = tickets.find_ticket(product, row["key"])
        prop = kb_extract.read_proposal(str(td)) if td else None
        if prop and prop.get("rules"):
            items.append({"ear": row["key"], "rules": prop["rules"],
                          "generated": prop.get("generated", "")})
    return render("kb_sprint.html", {
        "request": request, "user": user.public(), "nav": _nav("sprint", product),
        "date": date, "items": items, "has_any": bool(items),
        "busy": manager.is_busy(product, user.username),
    })


# ----------------------------- 工单详情 -----------------------------

def _build_ticket_ctx(request: Request, product: str, date: str, ear: str, tab: str) -> dict:
    tdir = tickets.ticket_dir(product, date, ear)
    if not tdir.exists():
        found = tickets.find_ticket(product, ear)
        if found:
            tdir = found
            date = tdir.parent.name
    exists = tdir.exists()
    badge = tickets.badge(tdir) if exists else {}

    has_questions = (tdir / "questions.md").exists()
    has_design = (tdir / "test-design.json").exists()
    has_spot = (tdir / "_spot-check.md").exists()
    # 精简后的 Tab：只留用户能看懂的，隐藏 AI 内部工程产物
    all_tabs = [
        {"key": "overview", "label": "工单说明", "present": exists},
        {"key": "questions", "label": "待确认", "present": has_questions},
        {"key": "tree", "label": "测试用例", "present": has_design},
        {"key": "spot-check", "label": "复核", "present": has_spot},
        {"key": "kb", "label": "知识回填", "present": has_design},
    ]
    if tab not in {t["key"] for t in all_tabs if t["present"]}:
        tab = next((t["key"] for t in all_tabs if t["present"]), "overview")

    ctx = {
        "request": request, "nav": _nav("ticket", product), "product": product,
        "date": date, "ear": ear, "tdir": str(tdir), "exists": exists,
        "badge": badge, "tabs": all_tabs, "active_tab": tab,
        # 「待确认」统一口径 = questions.md 未回答问题数（与看板列表/工单说明一致）
        "q_pending": (q_svc.parse(tdir / "questions.md").get("counts", {}).get("pending", 0)
                      if has_questions else 0),
        "needs_resume": q_svc.needs_resume(tdir) if exists else False,
        "strong_ok": config.strong_model_available()[0],
        "busy": manager.is_busy(product, user.username),
    }

    if tab == "overview" and exists:
        ctx["digest"] = digest.build(tdir)
    elif tab == "questions" and has_questions:
        qpath = tdir / "questions.md"
        ctx["questions"] = q_svc.parse(qpath)
        ctx["questions_mtime"] = qpath.stat().st_mtime_ns
    elif tab == "tree" and has_design:
        ctx["tree"] = tree.load(tdir / "test-design.json")
    elif tab == "spot-check" and has_spot:
        ctx["spotcheck_html"] = digest.spotcheck_html(tdir)
    elif tab == "kb" and has_design:
        from ..strong import kb_extract
        ctx["kb_proposal"] = kb_extract.read_proposal(str(tdir))
    return ctx


@router.get("/ticket/{product}/{date}/{ear}", response_class=HTMLResponse)
def ticket_detail(request: Request, product: str, date: str, ear: str,
                  tab: str = "", user=Depends(require_user)):
    if not (tickets.safe_product(product) and tickets.safe_ear(ear, product)):
        return RedirectResponse("/", status_code=303)
    ctx = _build_ticket_ctx(request, product, date, ear, tab)
    ctx["user"] = user.public()
    return render("ticket.html", ctx)


# ----------------------------- 配置 -----------------------------

@router.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request, product: str = DEFAULT_PRODUCT, saved: str = "", user=Depends(require_user)):
    ok, reason = strong_runner.availability_for(user_anthropic_endpoint(user))
    av = user.ai_view()

    def eff(kind: str) -> dict:
        a = av[kind]
        d = config.AI_DEFAULTS.get(kind, {})
        has_any = bool(a["base_url"] or a["model"] or a["has_key"])
        return {
            "provider": a["provider"] if has_any else d.get("provider", "anthropic"),
            "base_url": a["base_url"] or d.get("base_url", ""),
            "model": a["model"] or d.get("model", ""),
            "has_key": a["has_key"],
        }
    return render("settings.html", {
        "request": request, "user": user.public(), "nav": _nav("settings", product),
        "strong_ok": ok, "strong_reason": reason, "saved": saved,
        "ai": {"weak": eff("weak"), "strong": eff("strong")},
        "jira_url": user.jira_url or config.jira_default_url(),
        "jira_identity": user.jira_identity or {},
        "secrets": {"jira": bool(user.jira_pat), "weak": av["weak"]["has_key"], "strong": av["strong"]["has_key"]},
    })


@router.post("/settings/jira-pat", response_class=HTMLResponse)
def settings_set_pat(request: Request, jira_pat: str = Form(""), jira_url: str = Form(""),
                     clear: str = Form(""), user=Depends(require_user)):
    auth.store.set_jira_url(user.username, jira_url)  # 地址随表单保存（默认值也会落库，可改）
    if clear:
        auth.store.set_jira_pat(user.username, "")
        audit(user, "settings.jira_pat", "clear")
        return RedirectResponse("/settings?saved=patcleared", status_code=303)
    if jira_pat.strip():
        auth.store.set_jira_pat(user.username, jira_pat.strip())
        audit(user, "settings.jira_pat", "set")
    return RedirectResponse("/settings?saved=pat", status_code=303)


@router.get("/settings/secret/{kind}", response_class=JSONResponse)
def settings_secret(kind: str, user=Depends(require_user)):
    """Secret plaintext is never returned to the browser; users can replace or clear it."""
    if kind not in ("jira", "weak", "strong"):
        return JSONResponse({"ok": False}, status_code=400)
    return JSONResponse({"ok": False, "error": "凭证明文不可回显，请重新粘贴覆盖。"}, status_code=403)


@router.post("/settings/test-jira", response_class=JSONResponse)
def settings_test_jira(jira_url: str = Form(""), jira_pat: str = Form(""), user=Depends(require_user)):
    url = (jira_url or "").strip().rstrip("/") or config.jira_default_url()
    token = jira_pat.strip() or user.jira_pat
    if not url or not token:
        return JSONResponse({"ok": False, "error": "请先填写 Jira 地址和访问令牌"})
    env = dict(user_env(user))
    env["JIRA_URL"] = url
    env["JIRA_PERSONAL_TOKEN"] = token
    env.pop("JIRA_API_TOKEN", None)
    try:
        jf = scripts_loader.load_normal("jira_fetch")
        name = jf.myself(env) or "(未返回用户名)"
        identity = auth.store.set_jira_identity(user.username, name, url)
        audit(user, "settings.jira_test", f"ok {url} {name}")
        return JSONResponse({"ok": True, "name": name, "verified_at": identity.get("verified_at", "")})
    except SystemExit as e:
        return JSONResponse({"ok": False, "error": str(e) or "连接失败"})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})


@router.post("/settings/list-models", response_class=JSONResponse)
def settings_list_models(provider: str = Form("anthropic"), base_url: str = Form(""),
                         api_key: str = Form(""), kind: str = Form(""), user=Depends(require_user)):
    base_url = (base_url or "").strip()
    key = api_key.strip() or (auth.store.secret(user.username, kind) if kind in ("weak", "strong") else "")
    if not base_url or not key:
        return JSONResponse({"ok": False, "error": "请先填写接口地址和 API Key"})
    try:
        if provider == "openai":
            import openai
            cli = openai.OpenAI(base_url=base_url, api_key=key, timeout=30, max_retries=1)
            models = [getattr(m, "id", "") for m in cli.models.list().data]
        else:
            import anthropic
            cli = anthropic.Anthropic(base_url=base_url, api_key=key, timeout=30)
            models = [getattr(m, "id", "") for m in cli.models.list().data]
        models = sorted({m for m in models if m})[:200]
        if not models:
            return JSONResponse({"ok": False, "error": "接口返回空列表，请手动填写模型名"})
        return JSONResponse({"ok": True, "models": models})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"获取失败（可手动填写）：{type(e).__name__}: {str(e)[:160]}"})


@router.post("/settings/ai", response_class=HTMLResponse)
def settings_set_ai(request: Request, kind: str = Form(...), provider: str = Form("anthropic"),
                    base_url: str = Form(""), model: str = Form(""), api_key: str = Form(""),
                    user=Depends(require_user)):
    if kind not in ("weak", "strong"):
        return RedirectResponse("/settings?saved=aifail", status_code=303)
    prov = provider if provider in ("anthropic", "openai") else "anthropic"
    auth.store.set_ai(user.username, kind, base_url, model, api_key, provider=prov)
    audit(user, "settings.ai",
          f"{kind} provider={prov} base={'set' if base_url else ''} key={'set' if api_key else 'keep'}")
    return RedirectResponse("/settings?saved=ai", status_code=303)


@router.post("/settings/password", response_class=HTMLResponse)
def settings_password(request: Request, current: str = Form(...),
                      new: str = Form(...), user=Depends(require_user)):
    if not auth.store.authenticate(user.username, current):
        return RedirectResponse("/settings?saved=pwdfail", status_code=303)
    auth.store.set_password(user.username, new)
    audit(user, "settings.password")
    return RedirectResponse("/settings?saved=pwd", status_code=303)


def _config_view() -> dict:
    """配置只读视图（掩码，绝不回显明文）。"""
    cfg = config.raw_config()
    j = cfg.get("jira") or {}
    cheap = ((cfg.get("ai") or {}).get("cheap_provider")) or cfg.get("cheap_provider") or {}
    ep = config.anthropic_endpoint()

    def mask(v) -> str:
        return "已配置" if v and v not in ("REPLACE_ME", "REPLACE_ME_OR_LEAVE_BLANK") else "未配置"

    return {
        "jira_url": j.get("url") if j.get("url") not in (None, "REPLACE_ME") else "未配置",
        "jira_service_pat": mask(j.get("personal_access_token")),
        "cheap_enabled": bool(cheap.get("enabled")),
        "cheap_base": cheap.get("base_url") or "未配置",
        "cheap_model": cheap.get("model") or "未配置",
        "cheap_key": mask(cheap.get("api_key")),
        "anthropic_base": ep.get("base_url") or "未配置",
        "anthropic_model": ep.get("model") or "未配置",
        "anthropic_key": "已配置" if ep.get("api_key") else "未配置",
    }
