"""作业路由：弱链生成 / 重新选单 / 强模型抽检 / 证据消解 + 进度（SSE + 轮询片段）。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .. import config
from ..deps import audit, invoke, render, require_user, user_anthropic_endpoint, user_env
from ..jobs import manager
from ..services import selection, tickets
from ..strong import runner as strong_runner
from core.productcfg import DEFAULT_PRODUCT

router = APIRouter(prefix="/jobs")

# 生成模式 → run_sprint.py 额外参数
_MODES = {
    "select": lambda d: ["--sprint", d, "--select"],                       # 全量(未接 UI)
    # 生成用例 = 先出一版【草稿用例】：fetch..questions..points..draft，停在草稿复核+人工回答前。
    # 草稿写 _draft-design.json（看板不可见），强模型随后复核它、把需人工的点并入 questions.md。
    "draft": lambda d: ["--sprint", d, "--select", "--until", "draft"],
    # 继续生成 = 据已答 questions 出正式 test-design.json（再自动语义复核+文字修复）。
    "resume": lambda d: ["--sprint", d, "--select", "--resume-after-questions"],
    # 重新生成 = 从 Jira 重抓、重做到草稿、清掉旧 test-design/待确认答案，回到「待确认」。
    "regen": lambda d: ["--sprint", d, "--select", "--until", "draft", "--fresh"],
    "select_until_questions": lambda d: ["--sprint", d, "--select", "--until", "questions"],  # 兼容/降级
    "rerun": lambda d: ["--sprint", d],  # 全量重跑（未接入 UI）
}
_MODE_LABEL = {
    "select": "生成用例",
    "draft": "生成用例",
    "select_until_questions": "分步生成",
    "resume": "继续生成",
    "regen": "重新生成",
    "rerun": "重跑全部",
}


def _panel(request: Request, job) -> HTMLResponse:
    return render("partials/_job_panel.html",
                                      {"request": request, "job": job.public(), "lines": job.lines})


def _error_fragment(request: Request, message: str) -> HTMLResponse:
    return render("partials/_validate_result.html", {
        "request": request, "title": "无法启动", "result": {"ok": False, "error": message, "issues": []}})


def _bad_params(product: str, date: str) -> bool:
    """product/date 是 Form 字段、不受路由单段约束，必须白名单校验防路径穿越写盘。"""
    return not (tickets.safe_product(product) and tickets.safe_date(date))


def _need_config(field: str) -> Response:
    """返回 204 + HX-Trigger（仅 ASCII 字段名），前端据此弹窗引导去设置填写对应配置。"""
    return Response(status_code=204,
                    headers={"HX-Trigger": json.dumps({"qaNeedConfig": {"field": field}})})


def _has_weak_key(user) -> bool:
    return bool(((getattr(user, "ai", None) or {}).get("weak") or {}).get("api_key"))


def _scope_to_keys(extra: list[str], key_list: list[str]) -> list[str]:
    """把「按 sprint 选单(--select)」的参数缩到指定工单(--keys)：替换 --select；无则追加。
    供单工单生成/重新生成用——只跑这些工单、其余阶段标志(--until/--resume-after-questions)不变。"""
    if not key_list:
        return list(extra)
    out, joined = list(extra), ",".join(key_list)
    if "--select" in out:
        i = out.index("--select")
        out[i:i + 1] = ["--keys", joined]
    else:
        out += ["--keys", joined]
    return out


# ----------------------------- 弱链生成 -----------------------------

@router.post("/generate", response_class=HTMLResponse)
async def generate(request: Request, product: str = Form(...), date: str = Form(...),
                   mode: str = Form("select"), force: str = Form(""), keys: str = Form(""),
                   user=Depends(require_user)):
    if mode not in _MODES:
        return _error_fragment(request, f"未知生成模式：{mode}")
    if _bad_params(product, date):
        return _error_fragment(request, "产品或 Sprint 日期有误")
    if not _has_weak_key(user):
        return _need_config("weak")
    key_list = [k.strip() for k in (keys or "").split(",") if k.strip()]
    if key_list and not all(tickets.safe_ear(k, product) for k in key_list):
        return _error_fragment(request, "工单号有误")
    extra = _scope_to_keys(_MODES[mode](date), key_list)
    label = _MODE_LABEL.get(mode, mode)
    if key_list:  # 单/多工单：标签带上工单号
        label += " · " + ("、".join(key_list) if len(key_list) <= 2 else f"{len(key_list)} 单")
    if force:
        extra.append("--force")
    # 生成成功后自动接强模型阶段：生成用例/重新生成(出草稿) → "review"（预答 + 草稿复核，把需人工的
    # 点并入 questions.md，让人工只答一次）；继续生成(出正式用例) → "spot-check"（语义复核 + 文字
    # 自动修复）。强模型不可用则不接（用户仍可手动「复核」）。用户要求自动、不用手点。
    strong_ok = strong_runner.availability_for(user_anthropic_endpoint(user))[0]
    post_strong = None
    if strong_ok:
        if mode in ("draft", "regen", "select_until_questions"):
            post_strong = "review"
        elif mode == "resume":
            post_strong = "finalize"
    job, err = manager.start_generate(product, date, extra, label, user,
                                      post_strong=post_strong, post_keys=key_list)
    if err:
        return _error_fragment(request, err)
    audit(user, "job.generate", f"{product} {date} {mode} keys={keys or 'ALL'} post={post_strong}")
    return _panel(request, job)


# ----------------------------- 同步 Sprint（从 Jira 选择，不手输） -----------------------------

@router.get("/jira-sprints", response_class=HTMLResponse)
def jira_sprints(request: Request, product: str = DEFAULT_PRODUCT, user=Depends(require_user)):
    if not tickets.safe_product(product):
        return _error_fragment(request, "产品有误")
    if not user.jira_pat:
        return _need_config("jira")
    out = invoke(selection.list_jira_sprints, product, user_env(user))
    if not out["ok"]:
        # 不预设原因（旧文案一律写「检查网络/凭证」，会把"看板 id 未配置"等配置问题误导成网络故障）；
        # 直接呈现真实错误，由底层信息说明到底是网络、凭证还是配置缺失。
        return _error_fragment(request, f"读取 Jira Sprint 列表失败：{out['error']}")
    have = set(selection.sprint_dates(product))  # 已同步/已生成的 Sprint 不再出现在新增列表
    sprints = [s for s in out["data"] if s["date"] not in have]
    return render("partials/_sprint_picker.html",
                  {"request": request, "product": product, "sprints": sprints})


@router.post("/new-sprint", response_class=HTMLResponse)
def new_sprint(request: Request, product: str = Form(...), date: str = Form(...),
               user=Depends(require_user)):
    if _bad_params(product, date):
        return _error_fragment(request, "请填写正确的 Sprint 日期（形如 2026-06-16）。")
    if not user.jira_pat:
        return _need_config("jira")
    env = user_env(user)
    out = invoke(selection.run_selection, product, date, env, None, user.display_name)
    audit(user, "job.new-sprint", f"{product} {date} ok={out['ok']}")
    if not out["ok"]:
        return _error_fragment(request, f"没能从 Jira 找到该 Sprint 的工单：{out['error']}")
    resp = Response(status_code=204)
    resp.headers["HX-Redirect"] = f"/sprint/{product}/{date}"
    return resp


# ----------------------------- 重新选单（in-process，只读 Jira） -----------------------------

@router.post("/select", response_class=HTMLResponse)
def rerun_select(request: Request, product: str = Form(...), date: str = Form(...),
                 user=Depends(require_user)):
    if _bad_params(product, date):
        return _error_fragment(request, "产品或 Sprint 日期有误")
    if not user.jira_pat:
        return _need_config("jira")
    env = user_env(user)
    out = invoke(selection.run_selection, product, date, env, None, user.display_name)
    audit(user, "job.select", f"{product} {date} ok={out['ok']}")
    if out["ok"]:
        resp = Response(status_code=204)
        resp.headers["HX-Redirect"] = f"/sprint/{product}/{date}"
        return resp
    return _error_fragment(request, f"同步失败：{out['error']}")


# ----------------------------- 删除 Sprint（产物入回收站 + 清状态/账本/归属） -----------------------------

@router.post("/delete-sprint", response_class=HTMLResponse)
def delete_sprint(request: Request, product: str = Form(...), date: str = Form(...),
                  user=Depends(require_user)):
    if _bad_params(product, date):
        return _error_fragment(request, "产品或 Sprint 日期有误")
    # 抢占产品串行锁：整个删除期间与生成/复核互斥，避免边删边写出半个 Sprint 目录
    if not manager.try_lock(product, f"delete:{date}", user.username):
        return _error_fragment(request, "该产品有任务在运行，请等其完成后再删除。")
    try:
        out = invoke(selection.delete_sprint, product, date)
    finally:
        manager.unlock(product, user.username)
    if not out["ok"]:
        return _error_fragment(request, f"删除失败：{out['error']}")
    audit(user, "job.delete-sprint", f"{product} {date} tickets={out['data'].get('tickets', 0)}")
    resp = Response(status_code=204)
    resp.headers["HX-Redirect"] = f"/?product={product}"
    return resp


# ----------------------------- 强模型审计 -----------------------------

def _run_dirs(product: str, date: str, need: str) -> tuple[list[str], list[str]]:
    """返回 (ticket_dirs, ears)：本 sprint run_list 中含指定产物的工单。"""
    bd = selection.board(product, date)
    dirs, ears = [], []
    for row in bd["rows"]:
        if not row.get("is_run"):
            continue
        td = tickets.find_ticket(product, row["key"])
        if not td:
            continue
        if need == "design" and not (td / "test-design.json").exists():
            continue
        if need == "questions" and not (td / "questions.md").exists():
            continue
        dirs.append(str(td))
        ears.append(row["key"])
    return dirs, ears


@router.post("/spot-check", response_class=HTMLResponse)
async def spot_check(request: Request, product: str = Form(...), date: str = Form(...),
                     keys: str = Form(""), user=Depends(require_user)):
    if _bad_params(product, date):
        return _error_fragment(request, "产品或 Sprint 日期有误")
    ok, _reason = strong_runner.availability_for(user_anthropic_endpoint(user))
    if not ok:
        return _need_config("strong")
    dirs, ears = _run_dirs(product, date, "design")
    key_list = [k.strip() for k in (keys or "").split(",") if k.strip()]
    if key_list:  # 单/多工单复核：只保留指定工单
        keep = set(key_list)
        pairs = [(d, e) for d, e in zip(dirs, ears) if e in keep]
        dirs = [d for d, _ in pairs]
        ears = [e for _, e in pairs]
    if not dirs:
        return _error_fragment(request, "本期没有可复核的用例。")
    label = f"复核 {len(dirs)} 单" if not key_list else "复核 · " + (
        "、".join(ears) if len(ears) <= 2 else f"{len(ears)} 单")
    # finalize = 结构坏的先强模型兜底修复，再语义复核 + 文字自动修复（手点「复核」与定稿后自动一致）
    job, err = await manager.start_strong("finalize", product, dirs, label, user, sprint=date)
    if err:  # 此时只剩“产品忙”
        return _error_fragment(request, err)
    audit(user, "job.spot-check", f"{product} {date} {len(dirs)}单 keys={keys or 'ALL'}")
    return _panel(request, job)


@router.post("/resolve", response_class=HTMLResponse)
async def resolve(request: Request, product: str = Form(...), date: str = Form(...),
                  keys: str = Form(""), user=Depends(require_user)):
    if _bad_params(product, date):
        return _error_fragment(request, "产品或 Sprint 日期有误")
    ok, _reason = strong_runner.availability_for(user_anthropic_endpoint(user))
    if not ok:
        return _need_config("strong")
    dirs, ears = _run_dirs(product, date, "questions")
    key_list = [k.strip() for k in (keys or "").split(",") if k.strip()]
    if key_list:  # 单/多工单预答：只保留指定工单
        keep = set(key_list)
        pairs = [(d, e) for d, e in zip(dirs, ears) if e in keep]
        dirs = [d for d, _ in pairs]
        ears = [e for _, e in pairs]
    if not dirs:
        return _error_fragment(request, "本期没有可预答的待确认。")
    label = f"预答 {len(dirs)} 单" if not key_list else "预答 · " + (
        "、".join(ears) if len(ears) <= 2 else f"{len(ears)} 单")
    job, err = await manager.start_strong("resolve", product, dirs, label, user, sprint=date)
    if err:
        return _error_fragment(request, err)
    audit(user, "job.resolve", f"{product} {date} {len(dirs)}单 keys={keys or 'ALL'}")
    return _panel(request, job)


# ----------------------------- 知识回填（提炼候选规则 → 人工勾选 → 入库共享 KB） -----------------------------

@router.post("/kb-extract", response_class=HTMLResponse)
async def kb_extract_propose(request: Request, product: str = Form(...), date: str = Form(...),
                             keys: str = Form(""), user=Depends(require_user)):
    """强模型提炼本工单可入库的业务规则，写 kb-proposal.json（不入库）。完成后到工单「知识回填」Tab 勾选入库。"""
    if _bad_params(product, date):
        return _error_fragment(request, "产品或 Sprint 日期有误")
    ok, _reason = strong_runner.availability_for(user_anthropic_endpoint(user))
    if not ok:
        return _need_config("strong")
    dirs, ears = _run_dirs(product, date, "design")
    key_list = [k.strip() for k in (keys or "").split(",") if k.strip()]
    if key_list:
        keep = set(key_list)
        pairs = [(d, e) for d, e in zip(dirs, ears) if e in keep]
        dirs = [d for d, _ in pairs]
        ears = [e for _, e in pairs]
    if not dirs:
        return _error_fragment(request, "没有可提炼的用例（请先生成用例）。")
    label = "知识回填 · " + ("、".join(ears) if len(ears) <= 2 else f"{len(ears)} 单")
    job, err = await manager.start_strong("kb-extract", product, dirs, label, user, sprint=date)
    if err:
        return _error_fragment(request, err)
    audit(user, "job.kb-extract", f"{product} {date} {len(dirs)}单 keys={keys or 'ALL'}")
    resp = Response(status_code=204)
    resp.headers["HX-Redirect"] = f"/sprint/{product}/{date}"   # 到看板看任务进度，完成后回工单 Tab 勾选
    return resp


@router.post("/kb-apply", response_class=HTMLResponse)
def kb_apply(request: Request, product: str = Form(...), date: str = Form(...), ear: str = Form(...),
             idx: list[str] = Form(default=[]), user=Depends(require_user)):
    """把用户勾选的候选规则确定性追加进共享 rules.md（带 .bak + 目录校验 + 失败回滚）。"""
    from ..strong import kb_extract
    if not (tickets.safe_product(product) and tickets.safe_date(date) and tickets.safe_ear(ear, product)):
        return _error_fragment(request, "参数有误")
    tdir = tickets.find_ticket(product, ear)
    proposal = kb_extract.read_proposal(str(tdir)) if tdir else None
    if not proposal:
        return _error_fragment(request, "没有可应用的入库提案，请先在看板对该工单点「知识回填」生成。")
    sel = {int(x) for x in idx if str(x).strip().isdigit()}
    rules = [r for i, r in enumerate(proposal.get("rules") or []) if i in sel]
    if not rules:
        return _error_fragment(request, "请先勾选要入库的规则。")
    out = invoke(kb_extract.apply, product, ear, rules)
    if not out["ok"]:
        return _error_fragment(request, f"入库失败：{out['error']}")
    res = out["data"]
    if not res.get("ok"):
        return _error_fragment(request, res.get("notes") or "入库未完成。")
    audit(user, "job.kb-apply", f"{product} {date} {ear} applied={res.get('applied')}")
    return render("partials/_validate_result.html", {
        "request": request, "title": "已入库",
        "result": {"ok": True, "error": "", "issues": [
            {"level": "INFO", "message": f"已把 {res.get('applied')} 条规则写入共享知识库（§{res.get('chapter')} 知识回填），"
                                         f"原知识库已自动备份；今后生成会用到这些规则。"}]}})


@router.post("/kb-apply-batch", response_class=HTMLResponse)
def kb_apply_batch(request: Request, product: str = Form(...), date: str = Form(...),
                   item: list[str] = Form(default=[]), user=Depends(require_user)):
    """跨工单批量入库：item=['EAR-1:0','EAR-2:1',…]，按工单分组、逐单确定性写入共享 rules.md。"""
    from ..strong import kb_extract
    if not (tickets.safe_product(product) and tickets.safe_date(date)):
        return _error_fragment(request, "参数有误")
    by_ear: dict[str, set] = {}
    for it in item:
        if ":" not in str(it):
            continue
        ear, idx = str(it).rsplit(":", 1)
        if tickets.safe_ear(ear, product) and idx.strip().isdigit():
            by_ear.setdefault(ear, set()).add(int(idx))
    if not by_ear:
        return _error_fragment(request, "请先勾选要入库的规则。")
    applied, failed = 0, []
    for ear, idxs in by_ear.items():
        td = tickets.find_ticket(product, ear)
        prop = kb_extract.read_proposal(str(td)) if td else None
        if not prop:
            failed.append(ear)
            continue
        rules = [r for i, r in enumerate(prop.get("rules") or []) if i in idxs]
        out = invoke(kb_extract.apply, product, ear, rules)
        if out["ok"] and (out["data"] or {}).get("ok"):
            applied += out["data"].get("applied", 0)
        else:
            failed.append(ear)
    audit(user, "job.kb-apply-batch", f"{product} {date} applied={applied} failed={len(failed)}")
    msg = f"已把 {applied} 条规则写入共享知识库。"
    if failed:
        msg += f" 有 {len(failed)} 个工单未成功（{'、'.join(failed)}），可单独重试。"
    return render("partials/_validate_result.html", {
        "request": request, "title": "批量入库完成" if not failed else "部分入库",
        "result": {"ok": not failed, "error": msg if failed else "",
                   "issues": [{"level": "INFO", "message": msg}] if not failed else []}})


# ----------------------------- 进度 -----------------------------

@router.get("/{job_id}/log", response_class=HTMLResponse)
def job_log(request: Request, job_id: str, user=Depends(require_user)):
    job = manager.get(job_id, owner_username=user.username)
    if not job:
        return _error_fragment(request, "任务不存在或已过期。")
    return _panel(request, job)


@router.get("/{job_id}", response_class=JSONResponse)
def job_status(job_id: str, user=Depends(require_user)):
    job = manager.get(job_id, owner_username=user.username)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({**job.public(), "lines": job.lines})


@router.get("/{job_id}/events")
async def job_events(job_id: str, user=Depends(require_user)):
    async def stream():
        sent = 0
        last_seq = 0
        while True:
            job = manager.get(job_id, owner_username=user.username)
            if not job:
                yield "event: error\ndata: 任务不存在\n\n"
                return
            db_entries = manager.log_entries_after(job_id, after_seq=last_seq, owner_username=user.username)
            if db_entries:
                for entry in db_entries:
                    yield f"data: {entry['line']}\n\n"
                    last_seq = max(last_seq, int(entry["seq"]))
                sent = len(job.lines)
            else:
                while sent < len(job.lines):
                    yield f"data: {job.lines[sent]}\n\n"
                    sent += 1
            if job.status != "running":
                yield f"event: done\ndata: {job.status}:{job.rc}\n\n"
                return
            await asyncio.sleep(0.6)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
