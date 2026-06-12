"""工单页动作端点（HTMX 片段）：答题保存、用例树校验/保存、Jira 实时刷新。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
import html as _html

from .. import config
from ..deps import audit, invoke, render, require_user, user_env
from ..services import artifacts, jira as jira_svc
from ..services import questions as q_svc
from ..services import scripts_loader
from ..services import tickets, tree

router = APIRouter(prefix="/ticket/{product}/{date}/{ear}")


def _qpath(product: str, date: str, ear: str) -> Path:
    return tickets.ticket_dir(product, date, ear) / "questions.md"


def _tdpath(product: str, date: str, ear: str) -> Path:
    return tickets.ticket_dir(product, date, ear) / "test-design.json"


def _questions_fragment(request: Request, product: str, date: str, ear: str,
                        result: dict | None = None) -> HTMLResponse:
    qpath = _qpath(product, date, ear)
    parsed = q_svc.parse(qpath)
    tdir = tickets.ticket_dir(product, date, ear)
    return render("partials/_questions_form.html", {
        "request": request, "product": product, "date": date, "ear": ear,
        "questions": parsed, "questions_mtime": qpath.stat().st_mtime_ns if qpath.exists() else 0,
        "result": result, "needs_resume": q_svc.needs_resume(tdir),
    })


# ----------------------------- 答题闸门 -----------------------------

@router.post("/questions", response_class=HTMLResponse)
async def save_questions(request: Request, product: str, date: str, ear: str,
                         user=Depends(require_user)):
    form = await request.form()
    try:
        client_mtime = int(form.get("mtime") or 0)
    except ValueError:
        client_mtime = 0
    qpath = _qpath(product, date, ear)
    parsed = await run_in_threadpool(q_svc.parse, qpath)
    answers: dict[int, str] = {}
    for b in parsed.get("blocks", []):
        n = b["num"]
        choice = form.get(f"q_{n}_choice")
        supp = (str(form.get(f"q_{n}_supplement") or "")).strip()
        if choice is not None:  # 选择题：选项 / 自填 + 可选补充
            if choice == "__custom__":
                base = (str(form.get(f"q_{n}_custom") or "")).strip()
            else:
                opt = next((o for o in b.get("options", []) if o["key"] == choice), None)
                base = f"{choice}. {opt['text']}" if opt else choice
            parts = [p for p in (base, (f"补充：{supp}" if supp else "")) if p]
            answers[n] = "\n".join(parts)
        elif f"q_{n}" in form:  # 自由文本题（无可解析选项）
            answers[n] = str(form.get(f"q_{n}"))
    result = await run_in_threadpool(q_svc.save_answers, qpath, answers, client_mtime)
    audit(user, "questions.save", f"{ear} ok={result.get('ok')} kind={result.get('kind','')}")
    if result.get("ok"):
        artifacts.mirror_file(qpath, owner_username=user.username)
        tickets.invalidate_badge(qpath.parent)
    return _questions_fragment(request, product, date, ear, result)


@router.post("/questions/normalize", response_class=HTMLResponse)
def normalize_questions(request: Request, product: str, date: str, ear: str,
                        user=Depends(require_user)):
    """legacy/异常形态一键规范化（写盘）后重载。"""
    qpath = _qpath(product, date, ear)
    res = {"ok": False}
    if qpath.exists():
        nq = scripts_loader.normalize_questions()
        out = invoke(nq.normalize_file, qpath, write=True)
        if out["ok"]:
            vq = scripts_loader.validate_questions()
            issues = vq.validate(qpath)
            fails = [i for i in issues if i.level == "FAIL"]
            res = {"ok": not fails, "kind": "normalize",
                   "error": None if not fails else "规范化后仍有 FAIL，需人工修整。",
                   "issues": [{"level": i.level, "message": i.message} for i in issues]}
        else:
            res = {"ok": False, "error": out["error"], "kind": "exc"}
    audit(user, "questions.normalize", f"{ear} ok={res.get('ok')}")
    if res.get("ok"):
        artifacts.mirror_file(qpath, owner_username=user.username)
    tickets.invalidate_badge(qpath.parent)
    return _questions_fragment(request, product, date, ear, res)


# ----------------------------- 用例树 -----------------------------

@router.post("/tree/validate", response_class=HTMLResponse)
def validate_tree(request: Request, product: str, date: str, ear: str,
                  raw: str = Form(...), user=Depends(require_user)):
    res = tree.validate_text(_tdpath(product, date, ear), raw)
    return render("partials/_validate_result.html", {
        "request": request, "result": res, "title": "预检结果（未落盘）"})


@router.post("/tree/save", response_class=HTMLResponse)
def save_tree(request: Request, product: str, date: str, ear: str,
              raw: str = Form(...), mtime: str = Form("0"), user=Depends(require_user)):
    try:
        client_mtime = int(mtime or 0)
    except ValueError:
        client_mtime = 0
    tdpath = _tdpath(product, date, ear)
    res = tree.save_raw(tdpath, raw, client_mtime)
    audit(user, "tree.save", f"{ear} ok={res.get('ok')} kind={res.get('kind','')}")
    if res.get("ok"):
        artifacts.mirror_file(tdpath, owner_username=user.username)
        tickets.invalidate_badge(tdpath.parent)
    # 保存后重渲染整个树面板（含最新校验标记）
    loaded = tree.load(tdpath)
    return render("partials/_tree.html", {
        "request": request, "product": product, "date": date, "ear": ear,
        "tree": loaded, "save_result": res})


# ----------------------------- Jira 实时刷新（按用户 PAT，只读） -----------------------------

@router.post("/jira-refresh", response_class=HTMLResponse)
def jira_refresh(request: Request, product: str, date: str, ear: str,
                 user=Depends(require_user)):
    env = user_env(user)
    out = invoke(jira_svc.live_summary, env, ear)
    audit(user, "jira.refresh", f"{ear} ok={out['ok']}")
    if out["ok"]:
        return HTMLResponse(f'<div class="artifact" style="margin-top:12px"><pre>{_html.escape(out["data"])}</pre></div>')
    return HTMLResponse(f'<div class="alert err">刷新失败：{_html.escape(out["error"])}</div>')
