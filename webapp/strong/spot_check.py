"""qa-spot-check.js 的 Python 端口（强模型对抗式抽检，只读）。

两阶段（对每单独立）：
  Stage 1 Check —— 5 维并行审查 → findings
  Stage 2 Verify —— 对每条发现对抗式核验 → verdict，剔除误报
确定性写 _spot-check.md（按 severity 分组）。arithmetic 维度额外用 recompute() 复核证据
里的「算式=值」断言（零代码执行）。爆炸半径最小：agent 只读（Read/Grep/Glob），无 Bash/Write。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .. import config
from . import runner, schemas, tools
from core.productcfg import DEFAULT_PRODUCT

_ARTS = ["test-design.json", "analysis.md", "requirement.md", "business-context.md",
         "questions.md", "test-points.md", "linked-issues.md"]

_ROOT_FP_TEXT = re.compile(r"根节点|root node|CodeArts 容器节点|全局容器")
_ROOT_FP_TICKET = re.compile(r"工单号|[A-Z][A-Z0-9]+-\d+|[A-Z]+-xxxxxx|ticket key")

_FINDINGS_HINT = json.dumps(schemas.FINDINGS_SCHEMA, ensure_ascii=False)
_VERDICT_HINT = json.dumps(schemas.VERDICT_SCHEMA, ensure_ascii=False)


def _ear(dir_str: str) -> str:
    return [p for p in re.split(r"[\\/]+", dir_str) if p][-1]


def _root_ticket_false_positive(f: dict) -> bool:
    text = "\n".join(str(f.get(k, "")) for k in
                     ("dimension", "test_point", "problem", "evidence", "suggested_fix"))
    return bool(_ROOT_FP_TEXT.search(text) and _ROOT_FP_TICKET.search(text))


def _arts_block(dir_str: str, names: list[str], cap: int = 4000) -> str:
    """把工件内容直接读出嵌入 prompt（headless 下避免 agent 逐文件 Read 的多轮慢往返）。"""
    from pathlib import Path
    out = []
    for f in names:
        p = Path(dir_str) / f
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        if len(txt) > cap:
            txt = txt[:cap] + f"\n…(已截断，共 {len(txt)} 字)"
        out.append(f"\n===== {f} =====\n{txt}")
    return "".join(out) or "（无工件）"


def _dim_prompt(ear: str, dim: dict, arts: str) -> str:
    extra = ("必须独立重算并在 evidence 附「输入→算式→你算的值→用例写的值→是否一致」。\n"
             if dim["key"] == "arithmetic" else "")
    return (
        f"你是资深 QA 评审，对工单 {ear} 的测试用例做【{dim['label']}】维度的严格审查。\n\n"
        f"审查重点：{dim['focus']}\n\n{extra}"
        f"【基线口径·重要】questions.md 的已确认答案是 L1 范围/方案来源的权威依据。当 requirement.md / "
        f"analysis.md 等中间工件仍保留生成时的「[待确认] / 无 L1 / 待方案确认」表述、而 questions.md 已就此"
        f"给出确认时：以 questions.md 已确认答案为准，**不要**据中间工件的历史快照报问题（如“方案状态未回填/"
        f"仍标待确认”）。本维度只评判 test-design.json 用例交付物本身是否与【已确认答案】一致；中间 markdown "
        f"工件的历史状态不是用例缺陷、也无法由改用例修复，不得作为发现。\n\n"
        f"以下是该工单的全部工件（已内嵌，无需读取文件）：\n{arts}\n\n"
        f"只报真问题，定位到具体测试点/切面并给证据；没问题就返回空 findings，绝不凑数。"
    )


def _verify_prompt(f: dict, arts: str) -> str:
    return (
        f"对抗式核验工单 {f.get('ear')} 的一条强抽检发现——尝试【反驳】它，别轻易采信。\n\n"
        f"维度：{f.get('dimension')}\n严重度(初判)：{f.get('severity')}\n测试点：{f.get('test_point')}\n"
        f"问题：{f.get('problem')}\n证据：{f.get('evidence')}\n建议：{f.get('suggested_fix')}\n\n"
        f"以下是工单工件（已内嵌）：\n{arts}\n\n"
        f"对照工件判断它是真问题还是误报（用例其实对/方案确实那样/纯风格）。涉算术请独立再算一遍。"
        f"【特别】若该发现实质是「中间工件(requirement/analysis)仍保留生成时的 [待确认]/无 L1/待方案确认 表述、"
        f"而 questions.md 已确认、test-design.json 用例已据此写对」——判 is_real=false（中间工件快照不是用例缺陷、"
        f"也改不了用例）。拿不准默认 is_real=false。"
    )


async def _check_dim(ear, dir_str, product, dim) -> dict:
    arts = _arts_block(dir_str, _ARTS)
    res = await runner.query_json(_dim_prompt(ear, dim, arts), shape_hint=_FINDINGS_HINT,
                                  allowed_tools=[])
    findings = (res or {}).get("findings") or []
    # arithmetic 维度：确定性复核证据里的算式=值（已计取整规则；仅对真正对不上者附 advisory）
    if dim["key"] == "arithmetic":
        for f in findings:
            claims = tools.verify_claims(str(f.get("evidence", "")))
            bad = [c for c in claims if not c["ok"]]
            if bad:
                f["evidence"] = str(f.get("evidence", "")) + " ｜独立复算(供参考，未必计入业务取整规则): " + \
                    "; ".join(f"{c['expr']}={c['computed']}(用例写{c['claimed']})" for c in bad)
    return {"ear": ear, "dim": dim["key"], "findings": findings}


async def _verify_one(f: dict, dir_str: str) -> dict:
    arts = _arts_block(dir_str, ["test-design.json", "analysis.md", "requirement.md", "questions.md"], cap=3000)
    v = await runner.query_json(_verify_prompt(f, arts), shape_hint=_VERDICT_HINT, allowed_tools=[])
    return {"finding": f, "verdict": v or {}}


async def _run_one(dir_str: str, product: str, on_log) -> dict:
    ear = _ear(dir_str)
    checks = await asyncio.gather(
        *[_check_dim(ear, dir_str, product, d) for d in schemas.SPOT_CHECK_DIMS],
        return_exceptions=True)
    all_findings = []
    checked_dims = 0
    errors = []
    for c in checks:
        if isinstance(c, dict):
            checked_dims += 1
            for f in c.get("findings", []):
                f = {**f, "ear": ear}
                if not _root_ticket_false_positive(f):
                    all_findings.append(f)
        elif isinstance(c, Exception):
            errors.append(f"{type(c).__name__}: {str(c)[:160]}")
            if on_log:
                on_log(f"{ear}: 某维度复核出错：{type(c).__name__}: {str(c)[:160]}")
    if errors and checked_dims == 0:
        raise RuntimeError("全部复核维度失败，未写入复核结果：" + "；".join(errors[:3]))
    if not all_findings:
        _write_md(Path(dir_str), ear, [], checked_dims, errors)
        if on_log:
            suffix = f"（{len(errors)} 个维度失败）" if errors else ""
            on_log(f"{ear}: 已检查 {checked_dims}/{len(schemas.SPOT_CHECK_DIMS)} 个维度，无发现 → 确认 0 条{suffix}")
        return {"ear": ear, "dir": dir_str, "confirmed": [], "checked": checked_dims, "errors": errors}

    verified = await asyncio.gather(*[_verify_one(f, dir_str) for f in all_findings],
                                    return_exceptions=True)
    confirmed = []
    for r in verified:
        if not isinstance(r, dict):
            continue
        v = r.get("verdict") or {}
        if v.get("is_real") and v.get("severity_adjusted") != "not-a-bug":
            confirmed.append({**r["finding"],
                              "severity_adjusted": v.get("severity_adjusted"),
                              "verify_reason": v.get("reasoning", "")})
    verify_errors = [f"{type(r).__name__}: {str(r)[:160]}" for r in verified if isinstance(r, Exception)]
    checked_findings = len([r for r in verified if isinstance(r, dict)])
    if verify_errors and checked_findings == 0:
        raise RuntimeError("全部候选发现核验失败，未写入复核结果：" + "；".join(verify_errors[:3]))
    _write_md(Path(dir_str), ear, confirmed, checked_dims, errors + verify_errors,
              checked_findings=checked_findings, candidate_findings=len(all_findings))
    if on_log:
        suffix = f"（{len(errors) + len(verify_errors)} 个步骤失败）" if errors or verify_errors else ""
        on_log(f"{ear}: 核验 {len(all_findings)} 条 → 确认 {len(confirmed)} 条真实问题{suffix}")
    # 复核完自动修复（用户要求一步到位，不再单独点「修复」）：按确认的发现改用例文字，
    # 修复摘要并入 _spot-check.md，复核 tab 直接可见。延迟导入避免 revise↔spot_check 循环。
    if confirmed:
        from . import revise
        try:
            fix = await revise.fix_one(dir_str, on_log)
            # 复核里「需人工拍板」的点 → 折进 questions.md 变成【可回答】的待确认，而不是往用例里盖
            # [待确认] 图章（死胡同）。用户回答后「继续生成」即可消解。
            fix["folded_questions"] = _fold_questions(Path(dir_str), fix.get("questions") or [], on_log)
            _append_fix_summary(Path(dir_str), fix)
        except Exception as e:  # noqa: BLE001
            fix = {"applied": 0, "notes": f"自动修复出错：{type(e).__name__}: {str(e)[:160]}",
                   "leftover": [{"finding": "自动修复未完成", "fix_type": "unknown",
                                 "why": "复核建议尚未经过可信修复闭环"}]}
            _append_fix_summary(Path(dir_str), fix)
            if on_log:
                on_log(f"{ear}: 自动修复出错（已写入复核报告）：{type(e).__name__}: {str(e)[:160]}")
    return {"ear": ear, "dir": dir_str, "confirmed": confirmed,
            "checked": checked_dims, "checked_findings": checked_findings,
            "errors": errors + verify_errors}


_SEV_ICON = {"high": "🔴 高", "medium": "🟡 中", "low": "⚪ 低", "not-a-bug": "—"}


def _write_failure_md(directory: Path, ear: str, error: str) -> None:
    lines = [
        f"# 复核结果 — {ear}",
        "",
        "> ⚠️ 本次复核失败，未得到可信复核结论。",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 原因：{error}",
        "",
        "请检查复核模型配置或稍后重试；不要把旧版零条提示当作成功复核结果。",
    ]
    (directory / "_spot-check.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_md(directory: Path, ear: str, confirmed: list[dict], checked: int,
              errors: Optional[list[str]] = None,
              checked_findings: Optional[int] = None,
              candidate_findings: Optional[int] = None) -> None:
    errors = errors or []
    summary = f"> 已检查 {checked}/{len(schemas.SPOT_CHECK_DIMS)} 个复核维度"
    if checked_findings is not None and candidate_findings is not None:
        summary += f" · 已核验 {checked_findings}/{candidate_findings} 条候选发现"
    summary += f" · 确认 {len(confirmed)} 条建议"
    L = [f"# 复核结果 — {ear}", "", summary, ""]
    if errors:
        L += ["⚠️ 复核未完整完成，以下步骤失败；请稍后重试或检查复核模型配置。", ""]
        L += [f"- {x}" for x in errors[:8]]
        L.append("")
    if not confirmed and not errors:
        L.append("✅ 未发现需修正的问题。")
    elif not confirmed:
        L.append("本次未确认可直接修正的建议。")
    else:
        for sev in ("high", "medium", "low"):
            group = [c for c in confirmed if c.get("severity_adjusted") == sev]
            if not group:
                continue
            L.append(f"## {_SEV_ICON.get(sev, sev)}")
            L.append("")
            for c in group:
                L.append(f"### [{c.get('dimension')}] {c.get('test_point')}")
                L.append(f"- 问题：{c.get('problem')}")
                L.append(f"- 证据：{c.get('evidence')}")
                L.append(f"- 建议：{c.get('suggested_fix')}")
                if c.get("verify_reason"):
                    L.append(f"- 核验：{c.get('verify_reason')}")
                L.append("")
    (directory / "_spot-check.md").write_text("\n".join(L).rstrip() + "\n", encoding="utf-8")


def _append_fix_summary(directory: Path, fix: dict) -> None:
    """把复核后『自动修复』结果并入 _spot-check.md（复核 tab 据此展示）。"""
    p = directory / "_spot-check.md"
    if not p.exists():
        return
    L = ["", "## 🔧 自动修复", ""]
    applied = fix.get("applied", 0)
    if applied:
        L.append(f"已按复核建议自动修正 {applied} 处用例文字。")
    else:
        L.append("本次没有可仅靠改文字自动修复的项。")
    if fix.get("notes"):
        L.append(f"说明：{fix['notes']}")
    folded = fix.get("folded_questions") or {}
    qs = folded.get("accepted") or []
    skipped = folded.get("skipped") or []
    if qs:
        L += ["", "需你确认（已转成「待确认」问题，去工单回答后点「继续生成」即可消解，**不会留在用例里**）："]
        L += [f"- {x.get('question', '')}" for x in qs]
    if skipped:
        L += ["", "以下复核建议未转成待确认：依据不够具体，已要求下次复核给出可追溯来源。"]
        L += [f"- {x.get('question', '')}" for x in skipped]
    unfix = fix.get("unfixable") or []
    if unfix:
        L += ["", "需重新生成或人工补（仅改文字无法修复）："]
        L += [f"- {u.get('finding', '')} —— {u.get('why', '')}" for u in unfix]
    leftover = fix.get("leftover") or []
    if leftover:
        L += ["", "修后验收仍需处理："]
        for item in leftover:
            kind = item.get("fix_type", "unknown")
            where = f"（节点 {item.get('node_id')}）" if item.get("node_id") else ""
            L.append(f"- [{kind}] {item.get('finding', '')}{where} —— {item.get('why', '')}")
    elif fix.get("postcheck") is not None and not fix.get("postcheck_error"):
        L += ["", "修后验收：已确认复核建议被修复或正确分流。"]
    if fix.get("postcheck_error"):
        L += ["", f"修后验收失败：{fix['postcheck_error']}"]
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write("\n".join(L).rstrip() + "\n")
    except Exception:  # noqa: BLE001
        pass


_TRACEABLE_SOURCE_RE = re.compile(
    r"需求方案|修改方案|解决方案|关联工单|评论|业务规则|rules\.md|requirement\.md|"
    r"business-context\.md|linked-issues\.md|questions\.md|待确认答案|Jira\s*[A-Z][A-Z0-9]+-\d+|[A-Z][A-Z0-9]+-\d+"
)
_SOURCE_ONLY_RE = re.compile(r"^(?:复核发现|草稿复核|方案未明确|证据完整性审查|AI|模型|建议)[^“”\"']*$")


def _traceable_source(source: str) -> bool:
    """True only when the source names a user-findable artifact/comment/rule, not an AI-only suggestion."""
    src = (source or "").strip()
    if not src or _SOURCE_ONLY_RE.match(src):
        return False
    return bool(_TRACEABLE_SOURCE_RE.search(src))


def _fold_questions(directory: Path, questions: list, on_log) -> dict:
    """把复核判定『需人工拍板』的点折进 questions.md（可回答），而非塞进用例文字。复用 resolve 写回
    （normalize+validate+回滚+按题面去重）。延迟导入避免 resolve↔spot_check 循环。"""
    result = {"accepted": [], "skipped": [], "counts": {}}
    if not questions:
        return result
    q = directory / "questions.md"
    if not q.exists():
        return result
    items = []
    for x in questions:
        ques = (x.get("question") or "").strip()
        source = (x.get("source") or "").strip()
        if not ques:
            continue
        if not _traceable_source(source):
            result["skipped"].append(x)
            continue
        item = {
            "question": ques, "status": "needs_human", "already_answered": False,
            "problem": x.get("problem", "") or "复核发现：写确定用例所必需",
            "source": source,
            "possible_scenarios": x.get("possible_scenarios") or [],
            "impact": x.get("impact", "") or "影响相关用例的预期判定",
            "reason": x.get("reason", "") or "复核发现：需人工确认后才能写确定用例",
        }
        items.append(item)
        result["accepted"].append(item)
    if not items:
        if on_log and result["skipped"]:
            on_log(f"{_ear(str(directory))}: {len(result['skipped'])} 处复核待确认因缺少可追溯依据，未写入待确认")
        return result
    from . import resolve
    counts = resolve.apply_writeback(q, [], items, {}, report=False)
    result["counts"] = counts
    if on_log:
        tail = f"，跳过 {len(result['skipped'])} 处无可追溯依据" if result["skipped"] else ""
        on_log(f"{_ear(str(directory))}: 复核把 {counts.get('added_questions', 0)} 处需人工的点"
               f"转成可回答的待确认（回答后「继续生成」消解）{tail}")
    return result


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
            on_log(f"{ear}: 复核失败，已写入失败报告：{msg}")
    if failures:
        sample = "；".join(f"{ear}: {msg}" for ear, msg in failures[:3])
        raise RuntimeError(f"{len(failures)} 单复核失败，未得到可信复核结论：{sample}")
    return ok_results
