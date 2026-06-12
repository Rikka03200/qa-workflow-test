"""草稿复核（webapp 专属，「生成 → 人工确认」之前跑 —— 让人工只答一次的关键一环）。

把强模型唯一一次「看真实用例的完整复核」从【人工回答之后】挪到【之前】：
  生成用例 先出一版草稿 _draft-design.json（看板不可见）→ 本模块在草稿上做 5 维复核 →
  对每条发现【对抗式判定】是否「必须人工拍板才能写对用例」→ 是则渲染成给人工的问题，
  折进 questions.md（复用 resolve.apply_writeback 的写回+校验+回滚+去重）。

铁律：
- 只产【人工问题】，不改草稿（草稿后续会被「继续生成」整份重生成）。
- 默认 needs_human=false：纯结构/自包含/算术/可由 L1 方案或证据确定的、UI 文案都不问。
- 绝不把草稿里的 [待确认] 标记当问题「收割」——独立按方案/规则判定是否真需人工（守住
  CLAUDE.md §4「禁止从 test-design.json 收割待确认回填」）。
- 无 _draft-design.json（旧的 questions 阶段工单）→ 显式记录并退化到「只 resolve」。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import resolve, runner, schemas
from .spot_check import _arts_block, _ear, _traceable_source
from core.productcfg import DEFAULT_PRODUCT

DRAFT = "_draft-design.json"
_ARTS = [DRAFT, "requirement.md", "analysis.md", "business-context.md",
         "linked-issues.md", "questions.md", "test-points.md", "_jira-search.md"]
_VERIFY_ARTS = [DRAFT, "requirement.md", "analysis.md", "business-context.md",
                "linked-issues.md", "questions.md", "test-points.md", "_jira-search.md"]

_FINDINGS_HINT = json.dumps(schemas.FINDINGS_SCHEMA, ensure_ascii=False)
_DQ_HINT = json.dumps(schemas.DRAFT_QUESTION_SCHEMA, ensure_ascii=False)


def _dim_prompt(ear: str, dim: dict, arts: str) -> str:
    return (
        f"你是资深 QA 评审。工单 {ear} 现在有一版【草稿用例】(_draft-design.json，弱模型所写、尚未定稿)。"
        f"请就【{dim['label']}】维度审查这版草稿，目的【不是修用例】，而是找出"
        f"「要把用例写正确/写确定，还缺哪些必须由人来拍板的业务事实」。\n\n"
        f"审查重点：{dim['focus']}\n\n"
        f"以下是该工单全部工件（已内嵌，无需读取文件）：\n{arts}\n\n"
        f"只报真问题、定位到具体测试点/切面并给证据；没有就返回空 findings，绝不凑数。"
        f"注意：UI 文案、纯格式/自包含问题不在本步范围（那些定稿前会自动处理）。"
    )


def _to_question_prompt(ear: str, f: dict, arts: str) -> str:
    return (
        f"对抗式判定工单 {ear} 草稿复核的一条发现：写对这条用例，是否【必须人工/产品/原型拍板】？\n\n"
        f"维度：{f.get('dimension')}\n测试点：{f.get('test_point')}\n"
        f"问题：{f.get('problem')}\n证据：{f.get('evidence')}\n建议：{f.get('suggested_fix')}\n\n"
        f"以下是工单工件（已内嵌）：\n{arts}\n\n"
        f"判定规则（从严，默认 needs_human=false；口径必须与定稿终检 revise.questions 一致）：\n"
        f"- 只有当『方案/业务规则本身不确定、缺它无法判定用例通过/失败、且 L1 方案+关联评论+rules+"
        f"_jira-search 都查不到定论』时，才 needs_human=true。\n"
        f"- 若草稿已经把未确认的业务事实写成确定 step/expect，不能判 false；必须转成 questions，"
        f"让人工在定稿前拍板。\n"
        f"- 【无 L1 方案要问】若发现指出『本工单无明确修改方案 / L1 范围待确认 / 用例把 L3 客户场景或历史规则"
        f"写成了确定 expect / 方案没有的规则被自动补全(如交集算法)』→ needs_human=true，"
        f"question 写『本工单无明确修改方案，请确认要测试的 L1 范围/规则（含相关商品/客户可见性等具体规则）』。\n"
        f"- 能由 L1 方案或既有证据确定的、纯结构/自包含/算术/越界(直接删即可)、UI 文案 → 一律 false。\n"
        f"- 不得仅因草稿里写了 [待确认] 就判 true；要独立按证据判断是否真需人工。\n"
        f"- 已在 questions.md 里问过的同一件事 → false（避免重复问）。\n"
        f"true 时给出 question/problem/possible_scenarios/impact/source；possible_scenarios 必须给 2 个以上可选项，"
        f"让用户不用纯手写。source 必须是用户能找得到的真实依据：需求方案原句、关联工单 EAR-xxxxxx 的方案/评论原话、"
        f"业务规则、已确认待确认答案，或 requirement.md 修改方案中明确缺失该规则的事实；"
        f"禁止写『草稿复核』『AI 建议』『方案未明确』这类不可追溯来源。"
    )


async def _review_dim(ear: str, dir_str: str, dim: dict, arts: str) -> list[dict]:
    res = await runner.query_json(_dim_prompt(ear, dim, arts), shape_hint=_FINDINGS_HINT,
                                  allowed_tools=[])
    return [{**f, "ear": ear, "dimension": f.get("dimension") or dim["label"]}
            for f in ((res or {}).get("findings") or [])]


async def _to_question(ear: str, f: dict, arts: str) -> dict:
    """把一条发现对抗式判定成 accepted / not_needs_human / untraceable_source。"""
    v = await runner.query_json(_to_question_prompt(ear, f, arts), shape_hint=_DQ_HINT,
                                allowed_tools=[])
    if not v or not v.get("needs_human") or not (v.get("question") or "").strip():
        return {"item": None, "reason": "not_needs_human"}
    source = (v.get("source") or "").strip()
    if not _traceable_source(source):
        return {"item": None, "reason": "untraceable_source",
                "question": (v.get("question") or "").strip(), "source": source,
                "problem": v.get("problem", "") or f.get("problem", "")}
    return {"item": {
        "question": v.get("question", "").strip(),
        "status": "needs_human",
        "already_answered": False,
        "problem": v.get("problem", "") or f.get("problem", ""),
        "source": source,
        "possible_scenarios": v.get("possible_scenarios") or [],
        "impact": v.get("impact", "") or "影响相关用例的步骤/预期判定",
        "reason": v.get("reasoning", "") or "草稿复核：写确定用例所必需、当前证据不足，需人工确认",
    }, "reason": "accepted"}


async def _run_one(dir_str: str, product: str, on_log) -> dict:
    ear = _ear(dir_str)
    d = Path(dir_str)
    if not (d / DRAFT).exists():
        if on_log:
            on_log(f"{ear}: 无草稿用例，跳过草稿复核")
        _write_md(d, ear, [], 0, "无草稿用例，已退化为只做证据预答。", skipped_no_draft=True)
        return {"ear": ear, "dir": dir_str, "added": 0, "skipped_no_draft": True}
    if not (d / "questions.md").exists():
        return {"ear": ear, "dir": dir_str, "added": 0, "notes": "无 questions.md"}

    arts = _arts_block(dir_str, _ARTS)
    dim_lists = await asyncio.gather(
        *[_review_dim(ear, dir_str, dim, arts) for dim in schemas.SPOT_CHECK_DIMS],
        return_exceptions=True)
    findings: list[dict] = []
    dim_errors: list[str] = []
    checked_dims = 0
    for dl in dim_lists:
        if isinstance(dl, list):
            checked_dims += 1
            findings.extend(dl)
        elif isinstance(dl, Exception):
            msg = f"{type(dl).__name__}: {str(dl)[:160]}"
            dim_errors.append(msg)
            if on_log:
                on_log(f"{ear}: 某维度草稿复核出错：{msg}")
    if checked_dims == 0:
        raise RuntimeError("全部草稿复核维度失败，未得到可信草稿复核结论：" + "；".join(dim_errors[:3]))
    if not findings:
        _write_md(d, ear, [], 0, "", reviewed=0, checked_dims=checked_dims, errors=dim_errors)
        if dim_errors:
            raise RuntimeError("草稿复核未完整完成，不能判定为无新增待确认：" + "；".join(dim_errors[:3]))
        if on_log:
            on_log(f"{ear}: 已检查 {checked_dims}/{len(schemas.SPOT_CHECK_DIMS)} 个草稿复核维度，无需人工的新问题")
        return {"ear": ear, "dir": dir_str, "added": 0, "checked": checked_dims, "errors": dim_errors}

    varts = _arts_block(dir_str, _VERIFY_ARTS, cap=6000)
    judged = await asyncio.gather(*[_to_question(ear, f, varts) for f in findings],
                                  return_exceptions=True)
    items: list[dict] = []
    skipped_untraceable: list[dict] = []
    judge_errors: list[str] = []
    not_human = 0
    judged_ok = 0
    for j in judged:
        if isinstance(j, dict):
            judged_ok += 1
            if isinstance(j.get("item"), dict):
                items.append(j["item"])
            elif j.get("reason") == "untraceable_source":
                skipped_untraceable.append(j)
            else:
                not_human += 1
        elif isinstance(j, Exception):
            judge_errors.append(f"{type(j).__name__}: {str(j)[:160]}")
    if judge_errors and judged_ok == 0:
        raise RuntimeError("全部草稿发现核验失败，未得到可信草稿复核结论：" + "；".join(judge_errors[:3]))

    counts = {"added_questions": 0, "notes": ""}
    if items:
        # 折进 questions.md（复用 resolve 的确定性写回：normalize+validate+回滚+按题面去重）。
        # report=False：不覆盖 resolve 写的 _resolve.md。
        counts = resolve.apply_writeback(d / "questions.md", [], items, {}, report=False)
    added = counts.get("added_questions", 0)
    _write_md(d, ear, items, added, counts.get("notes", ""), reviewed=len(findings),
              checked_dims=checked_dims, errors=dim_errors + judge_errors,
              skipped_untraceable=skipped_untraceable, not_human=not_human)
    if dim_errors or judge_errors:
        raise RuntimeError("草稿复核未完整完成，不能作为可信首次强检查结论：" +
                           "；".join((dim_errors + judge_errors)[:3]))
    if skipped_untraceable:
        if on_log:
            on_log(f"{ear}: 草稿复核有 {len(skipped_untraceable)} 条待确认候选缺少可追溯依据，未写入待确认")
        raise RuntimeError(f"草稿复核有 {len(skipped_untraceable)} 条待确认候选缺少可追溯依据，已写入 _draft-review.md")
    if on_log:
        tail = f"（{counts['notes']}）" if counts.get("notes") else ""
        if items:
            on_log(f"{ear}: 草稿复核 → 追加 {added} 条人工待确认{tail}")
        else:
            suffix = f"；{len(judge_errors)} 条核验失败" if judge_errors else ""
            on_log(f"{ear}: 草稿复核 {len(findings)} 条发现，经核验均无需人工{suffix}")
    return {"ear": ear, "dir": dir_str, "added": added, "reviewed": len(findings),
            "checked": checked_dims, "errors": dim_errors + judge_errors}


def _write_md(directory: Path, ear: str, items: list[dict], added: int, notes: str,
              reviewed: int = 0, checked_dims: int = 0, errors: Optional[list[str]] = None,
              skipped_untraceable: Optional[list[dict]] = None, not_human: int = 0,
              skipped_no_draft: bool = False) -> None:
    errors = errors or []
    skipped_untraceable = skipped_untraceable or []
    L = [f"# 草稿复核 — {ear}", ""]
    if skipped_no_draft:
        L += ["> 本工单没有草稿用例，已退化为只做证据预答。", ""]
    else:
        L += [f"> 已检查 {checked_dims}/{len(schemas.SPOT_CHECK_DIMS)} 个草稿复核维度 · "
              f"发现 {reviewed} 条 · 新增 {added} 条待确认", ""]
    if notes:
        L += [f"说明：{notes}", ""]
    if errors:
        L += ["⚠️ 草稿复核未完整完成，以下步骤失败；本次结果不能当作完整强检查。", ""]
        L += [f"- {x}" for x in errors[:8]]
        L.append("")
    if items:
        L.append("## 复核并入的待确认")
        for it in items:
            L.append(f"- {it.get('question')}")
        L.append("")
    if skipped_untraceable:
        L.append("## 未写入的待确认候选")
        L.append("以下候选缺少可追溯来源，已阻断本次草稿复核，避免把 AI 建议当依据：")
        for it in skipped_untraceable:
            src = it.get("source") or "无来源"
            L.append(f"- {it.get('question', '')}（来源：{src}）")
        L.append("")
    if not items and not skipped_untraceable and not errors and not skipped_no_draft:
        L.append("✅ 草稿复核未发现需要人工额外确认的点。")
    elif not items and not skipped_untraceable and not_human:
        L.append(f"{not_human} 条草稿发现经核验不属于需人工拍板的问题。")
    (directory / "_draft-review.md").write_text("\n".join(L).rstrip() + "\n", encoding="utf-8")


def _write_failure_md(directory: Path, ear: str, error: str) -> None:
    p = directory / "_draft-review.md"
    lines = [
        f"# 草稿复核 — {ear}",
        "",
        "> ⚠️ 本次草稿复核失败，未得到可信的首次强检查结论。",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 原因：{error}",
        "",
        "请重新执行生成后的草稿复核；不要把本次失败当作『没有新增待确认』。",
    ]
    body = "\n".join(lines).rstrip() + "\n"
    if p.exists():
        try:
            existing = p.read_text(encoding="utf-8").rstrip()
            if existing:
                body = existing + "\n\n## 失败状态\n\n" + "\n".join(lines[2:]).rstrip() + "\n"
        except Exception:  # noqa: BLE001
            pass
    p.write_text(body, encoding="utf-8")


async def run(ticket_dirs: list[str], product: str = DEFAULT_PRODUCT,
              on_log: Optional[Callable[[str], None]] = None) -> list[dict]:
    if not ticket_dirs:
        return []
    results = await asyncio.gather(*[_run_one(d, product, on_log) for d in ticket_dirs],
                                   return_exceptions=True)
    ok_results = []
    failures = []
    for dir_str, result in zip(ticket_dirs, results):
        if isinstance(result, dict):
            ok_results.append(result)
            continue
        ear = _ear(dir_str)
        msg = f"{type(result).__name__}: {str(result)[:220]}"
        failures.append((ear, msg))
        try:
            _write_failure_md(Path(dir_str), ear, msg)
        except Exception:  # noqa: BLE001
            pass
        if on_log:
            on_log(f"{ear}: 草稿复核失败，已写入失败报告：{msg}")
    if failures:
        sample = "；".join(f"{ear}: {msg}" for ear, msg in failures[:3])
        raise RuntimeError(f"{len(failures)} 单草稿复核失败，未得到可信首次强检查结论：{sample}")
    return ok_results
