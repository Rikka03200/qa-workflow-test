"""qa-resolve.js 的 Python 端口（证据消解，会写 questions.md —— 高风险）。

铁律（来自 plan §4.3）：LLM 只负责【判断/试答/核验】，**写盘交确定性 Python**：
- 只动「✅ 答案」区、需人工提示行、追加的连续 Q 题块；绝不修改人工已填答案。
- 写前备份 .bak；写后必须仍通过 normalize+validate，不过则回滚。
- 漏问绝不来自 test-design.json（prompt 已硬禁）。

⚠️ 上线前需 shadow-run 平价评审（用户已同意）；未配置 SDK 时路由降级为复制命令。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .. import config
from . import runner, schemas
from .spot_check import _arts_block
from ..services import questions as q_svc
from ..services import scripts_loader
from core.productcfg import DEFAULT_PRODUCT

INTRO_LINE = "> 请在每个“✅ 答案”下方填写确认结果；如果暂时无法确认，填写 `[待确认]`。"
_RESOLVE_HINT = json.dumps(schemas.RESOLVE_SCHEMA, ensure_ascii=False)
_VERIFY_HINT = json.dumps(schemas.VERIFY_SCHEMA, ensure_ascii=False)


def _ear(dir_str: str) -> str:
    return [p for p in re.split(r"[\\/]+", dir_str) if p][-1]


def _qnum(q: str):
    m = re.match(r"\s*Q(\d+)", q or "")
    return int(m.group(1)) if m else None


def _resolve_prompt(ear: str, arts: str) -> str:
    return (
        f"你是资深 QA。对工单 {ear} 做【证据消解 + 证据完整性审查】。人工回答前，把有据可查的问题"
        f"自动答掉，并检查 questions.md 是否漏掉“写确定用例所必需、但当前证据不足”的业务事实；只把真无据的留人工。\n\n"
        f"边界（必须遵守）：只基于下方内嵌工件的实际证据，不预演/不模拟/不按经验猜；不补方案外功能；"
        f"不把 L2/L3 当 L1 定论；已有人工答案为终裁，只标 already_answered=true 跳过。"
        f"【无 L1 铁律】若 requirement.md §3 修改方案 本身是 [待确认]/无可用 L1 规则：禁止用 L3 客户场景或历史 rules 把疑点消解成确定规则；这种情况下保留/新增一条 needs_human「本工单无明确修改方案，请确认要测试的 L1 范围/规则」，相关疑点一律 needs_human。"
        f"（注意：下方刻意不含 test-design/测试点，本步不做 JSON 收割。）\n\n"
        f"以下是该工单工件（已内嵌，无需读取文件）：\n{arts}\n\n"
        f"任务A 逐条处理 questions.md 的每个 ## Q（→ resolutions）：先看 ✅答案 是否已有人工内容→有则 already_answered=true、"
        f"status=resolved、answer 填用户原文（不改）；否则用 §3修改方案/关联评论/business-context 中的规则试答，能定论→resolved+answer+source，"
        f"穷尽仍无据或需原型/需产品→needs_human。\n"
        f"任务B 证据完整性审查（→ missing_questions）：仅当某 L1 切面写确定用例必需、缺它无法判定通过/失败、"
        f"当前 questions.md 与 analysis 已列问题未覆盖、且不是 UI 文案/方案外维度时，才新增；能定论则 resolved 否则 needs_human。\n"
        f"铁律：能查到就别留人工；绝不为消解而编造，answer 必须可被 source 逐字支撑。"
    )


def _verify_prompt(ear: str, item: dict, kind: str, arts: str) -> str:
    return (
        f"对抗式核验工单 {ear} 的一条【证据消解】结论——尝试反驳它。\n"
        f"类型：{'证据完整性新增' if kind == 'missing' else 'questions.md 既有题'}\n"
        f"问题：{item.get('question')}\n拟定答案：{item.get('answer')}\n声称出处：{item.get('source')}\n理由：{item.get('reason')}\n\n"
        f"以下是工单工件（已内嵌）：\n{arts}\n\n"
        f"对照工件核到原文：出处是否真实存在且语义确实指向该答案（非简化/类比/张冠李戴）？"
        f"是否把 L2/L3 当 L1？拿不准默认 supported=false。"
    )


_RESOLVE_ARTS = ["questions.md", "requirement.md", "analysis.md", "linked-issues.md",
                 "business-context.md", "_jira-search.md"]


async def _run_one(dir_str: str, product: str, on_log) -> dict:
    ear = _ear(dir_str)
    arts = _arts_block(dir_str, _RESOLVE_ARTS)
    r = await runner.query_json(_resolve_prompt(ear, arts), shape_hint=_RESOLVE_HINT, allowed_tools=[])
    resolutions = (r or {}).get("resolutions") or []
    missing = (r or {}).get("missing_questions") or []

    # Stage 2：核验每条「已据消解」（既有未答 + 新增 resolved）
    to_verify = [("existing", x) for x in resolutions
                 if x.get("status") == "resolved" and not x.get("already_answered")]
    to_verify += [("missing", x) for x in missing if x.get("status") == "resolved"]
    verdicts = await asyncio.gather(
        *[runner.query_json(_verify_prompt(ear, item, kind, arts), shape_hint=_VERIFY_HINT,
                            allowed_tools=[])
          for kind, item in to_verify], return_exceptions=True)
    vmap = {}
    for (kind, item), v in zip(to_verify, verdicts):
        if isinstance(v, dict):
            vmap[f"{kind}::{item.get('question')}"] = v

    # Stage 3：确定性写回
    counts = apply_writeback(Path(dir_str) / "questions.md", resolutions, missing, vmap)
    counts["ear"] = ear
    counts["dir"] = dir_str
    if on_log:
        on_log(f"{ear}: 新增 {counts['added_questions']} / 已预答 {counts['resolved_applied']} / "
               f"待人工 {counts['needs_human']} / 跳过已填 {counts['skipped_human_answered']}")
    return counts


def _final_state(x: dict, vmap: dict, kind: str) -> tuple[str, str]:
    """返回 (_final, reason)。镜像 qa-resolve.js 的 finalize。"""
    if x.get("already_answered"):
        return "human", ""
    if x.get("status") == "resolved":
        v = vmap.get(f"{kind}::{x.get('question')}")
        if v and v.get("supported"):
            return "resolved", ""
        return "needs_human", f"消解核验未通过，退回人工：{(v or {}).get('reasoning') or x.get('reason', '')}"
    return "needs_human", x.get("reason", "")


_SCENARIO_PREFIX = re.compile(r"^\s*(?:[-*•]\s*)?([A-Z])[.、)）]\s*(.+?)\s*$")


def _format_scenarios(scenarios) -> list[str]:
    """Render possible_scenarios as A./B. lines so the web form becomes radio options."""
    raw = [str(x).strip() for x in (scenarios or []) if str(x).strip()]
    out: list[str] = []
    for idx, text in enumerate(raw[:6]):
        m = _SCENARIO_PREFIX.match(text)
        key = chr(ord("A") + idx)
        label = m.group(2).strip() if m else text
        out.append(f"- {key}. {label}")
    if len(out) >= 2:
        return out
    return ["- A. 是", "- B. 否"]


def _render_missing_block(qn: int, item: dict, final: str, reason: str) -> list[str]:
    question = re.sub(r"^\s*Q\d+\s*[:：]\s*", "", item.get("question", "")).strip() or "（待确认）"
    source = item.get("source", "") or "证据完整性审查"
    problem = item.get("problem", "") or item.get("reason", "") or question
    impact = item.get("impact", "") or "影响相关测试步骤/预期判定"
    out = [f"## Q{qn}: {question}", f"**问题**：{problem}", f"**来源**：{source}"]
    if final == "resolved":
        out.append(f"**可能场景**：已由证据确定：{item.get('answer', '')}")
    else:
        out.append("**可能场景**：")
        out.extend(_format_scenarios(item.get("possible_scenarios") or []))
    out.append(f"**影响范围**：{impact}")
    out.append("**✅ 答案**：")
    if final == "resolved":
        out.append(f"{item.get('answer', '')}（据 {source} 自动消解）")
    else:
        out.append(f"> 已自动检索无据，需人工/产品确认：{(reason or item.get('reason', ''))[:120]}")
        out.append("<!-- 待填 -->")
    out.append("")
    return out


def apply_writeback(path: Path, resolutions: list, missing: list, vmap: dict,
                    report: bool = True) -> dict:
    """确定性写回 questions.md（只填空/不碰人工已填）+ .bak + 写后校验回滚。

    report=False：不写 _resolve.md（供 draft_review 二次追加问题时复用本机器、不覆盖 resolve 报告）。
    """
    counts = {"added_questions": 0, "resolved_applied": 0, "needs_human": 0,
              "skipped_human_answered": 0, "notes": ""}
    if not path.exists():
        counts["notes"] = "questions.md 不存在"
        return counts

    parsed = q_svc.parse(path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # 既有题写回。robust 匹配 + 按【文件实际行序】倒序改，保持行号有效。
    blocks = parsed.get("blocks", [])
    nums = [b["num"] for b in blocks]
    if len(set(nums)) != len(nums):
        # 题号重复（带外手改才会出现，规范文件会被 validate-questions FAIL）→ 放弃，绝不冒险错切
        counts["notes"] = "questions.md 题号重复，apply_writeback 放弃既有题写回（请先规范化）。"
        return counts

    def _norm_q(s: str) -> str:
        return re.sub(r"\s+", "", re.sub(r"^\s*Q\d+\s*[:：]\s*", "", s or ""))

    title_index: dict[str, list] = {}
    for b in blocks:
        title_index.setdefault(_norm_q(b["title"]), []).append(b)
    num_set = set(nums)

    # 把每条 resolution 唯一定位到一个既有题：① QN 精确命中 ② 题面文本唯一匹配 ③ 否则跳过（不按下标盲配）
    matched: dict[int, dict] = {}
    unmatched = 0
    for x in resolutions:
        q = x.get("question", "")
        n = _qnum(q)
        target = None
        if n is not None and n in num_set:
            target = next(b for b in blocks if b["num"] == n)
        else:
            cand = title_index.get(_norm_q(q)) or []
            if len(cand) == 1:
                target = cand[0]
        if target is None:
            unmatched += 1
            continue
        matched[target["num"]] = x

    for block in sorted(blocks, key=lambda b: (b["label_line"] if b["label_line"] is not None else -1),
                        reverse=True):
        x = matched.get(block["num"])
        if x is None:
            continue
        final, reason = _final_state(x, vmap, "existing")
        label = block["label_line"]
        if label is None:
            continue
        if final == "human" or block["state"] in ("human", "auto"):
            if block["state"] == "human":
                counts["skipped_human_answered"] += 1
            continue
        if final == "resolved":
            ans = f"{x.get('answer', '')}（据 {x.get('source', '证据')} 自动消解）"
            lines[label] = "**✅ 答案**："
            lines[label + 1:block["block_end"]] = [ans, ""]
            counts["resolved_applied"] += 1
        else:  # needs_human：保留占位，在答案标签上方补「已自动检索无据」提示（若本题尚无）
            counts["needs_human"] += 1
            start = next((i for i in range(label, -1, -1)
                          if re.match(r"^## Q\d+:", lines[i])), label)
            existing_hint = any(re.match(r"^>\s*已自动检索无据", lines[i].strip())
                                for i in range(start, label))
            if not existing_hint:
                lines[label:label] = [f"> 已自动检索无据，需人工/产品确认：{reason[:120]}", ""]
    if unmatched:
        counts["notes"] = (counts["notes"] + f" {unmatched} 条消解无法定位到既有题，已跳过（未按下标盲配）。").strip()

    # 若原文是“无”，先铺标题 + 说明行，再追加新增题
    existing_titles = {re.sub(r"\s+", "", b["title"]) for b in blocks}
    next_qn = (max(num_set) if num_set else 0)

    if parsed.get("form") == "none":
        lines = [f"# 待确认问题清单 — {path.parent.name}", "", INTRO_LINE, ""]
        next_qn = 0

    appended = []
    for item in missing:
        title_norm = re.sub(r"\s+", "", re.sub(r"^\s*Q\d+\s*[:：]\s*", "", item.get("question", "")))
        if title_norm and title_norm in existing_titles:
            continue  # 语义重复，跳过
        final, reason = _final_state(item, vmap, "missing")
        next_qn += 1
        appended += _render_missing_block(next_qn, item, final, reason)
        counts["added_questions"] += 1
        if final == "resolved":
            counts["resolved_applied"] += 1
        else:
            counts["needs_human"] += 1
        existing_titles.add(title_norm)

    if appended:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines += appended

    new_text = "\n".join(lines).rstrip() + "\n"

    # 写临时 → normalize → validate；不过则不动原文
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.resolve.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    nq = scripts_loader.normalize_questions()
    vq = scripts_loader.validate_questions()
    try:
        nq.normalize_file(tmp, write=True)
    except Exception:  # noqa: BLE001
        pass
    issues = vq.validate(tmp)
    if any(i.level == "FAIL" for i in issues):
        tmp.unlink(missing_ok=True)
        counts["notes"] = "写回后校验未通过，已放弃（原文未动）：" + \
            "; ".join(i.message for i in issues if i.level == "FAIL")[:300]
        # 回滚计数（未真正写入）
        counts.update({"added_questions": 0, "resolved_applied": 0})
        return counts
    # 备份原文 + 原子替换
    try:
        bak = path.with_name(path.name + ".bak")
        bak.write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    import os
    os.replace(tmp, path)
    if report:
        _write_resolve_md(path.parent, path.parent.name, resolutions, missing, vmap)
    return counts


def _write_resolve_md(directory: Path, ear: str, resolutions, missing, vmap) -> None:
    L = [f"# 证据消解报告 — {ear}", ""]

    def grp(items, kind, want):
        return [x for x in items if _final_state(x, vmap, kind)[0] == want]

    L.append("## 原有问题自动消解")
    rows = grp(resolutions, "existing", "resolved")
    L += [f"- {x.get('question')}：{x.get('answer')}（据 {x.get('source')}）" for x in rows] or ["- 无"]
    L.append("\n## 原有问题仍需人工")
    rows = grp(resolutions, "existing", "needs_human")
    L += [f"- {x.get('question')}：{x.get('reason', '')}" for x in rows] or ["- 无"]
    L.append("\n## 证据完整性审查新增并自动消解")
    rows = grp(missing, "missing", "resolved")
    L += [f"- {x.get('question')}：{x.get('answer')}（据 {x.get('source')}）" for x in rows] or ["- 无"]
    L.append("\n## 证据完整性审查新增仍需人工")
    rows = grp(missing, "missing", "needs_human")
    L += [f"- {x.get('question')}：{x.get('reason', '')}" for x in rows] or ["- 无"]
    L.append("\n## 人工已填（跳过）")
    rows = [x for x in resolutions if x.get("already_answered")]
    L += [f"- {x.get('question')}" for x in rows] or ["- 无"]
    (directory / "_resolve.md").write_text("\n".join(L).rstrip() + "\n", encoding="utf-8")


def _write_failure_md(directory: Path, ear: str, error: str) -> None:
    lines = [
        f"# 证据消解报告 — {ear}",
        "",
        "> ⚠️ 本次证据消解失败，未得到可信的首次强检查结论。",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 原因：{error}",
        "",
        "请重新执行生成后的预答/草稿复核；不要把本次失败当作『没有漏问』。",
    ]
    (directory / "_resolve.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


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
            on_log(f"{ear}: 预答失败，已写入失败报告：{msg}")
    if failures:
        sample = "；".join(f"{ear}: {msg}" for ear, msg in failures[:3])
        raise RuntimeError(f"{len(failures)} 单预答失败，未得到可信首次强检查结论：{sample}")
    return ok_results
