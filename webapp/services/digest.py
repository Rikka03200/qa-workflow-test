"""工单「大白话说明」：把工程产物汇总成普通用户能看懂的概览（确定性，无需 LLM）。

来源：_jira-summary.md(标题) + requirement.md §3 修改方案(产品需求原文，排版渲染) +
test-design.json(测试点清单) + questions.md(待确认数) + 校验徽章。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import humanize as _humanize
from . import questions as q_svc
from . import scripts_loader, summary_md, tickets
from core.productcfg import DEFAULT_PRODUCT, get_product


def _product_from_dir(directory: Path) -> str:
    try:
        return directory.parents[1].name
    except IndexError:
        return DEFAULT_PRODUCT


def _title(directory: Path) -> str:
    sm = tickets.read_artifact(directory, "_jira-summary.md") or ""
    ticket_re = get_product(_product_from_dir(directory)).ticket_key_regex.strip()
    if ticket_re.startswith("^"):
        ticket_re = ticket_re[1:]
    if ticket_re.endswith("$"):
        ticket_re = ticket_re[:-1]
    m = re.search(rf"^#\s*(?:{ticket_re})\s*[:：]\s*(.+)$", sm, flags=re.M)
    if m:
        return m.group(1).strip()
    req = tickets.read_artifact(directory, "requirement.md") or ""
    m = re.search(r"^#\s+(.+)$", req, flags=re.M)
    return m.group(1).strip() if m else directory.name


def _section(text: str, heading_re: str) -> str:
    m = re.search(heading_re, text, flags=re.M)
    if not m:
        return ""
    nxt = re.search(r"^##\s+", text[m.end():], flags=re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[m.end():end].strip()


# 需求方案里给 AI 看的模板指令样板（不该出现在用户视图）——按子串过滤掉
_PLAN_NOISE = (
    "唯一权威来源", "围绕本段", "本段编写", "测试用例必须", "降级为", "L2/L3", "L1",
    "拿不准", "仅作背景", "仅供", "待人工", "本工单业务规则的",
)


def _plan_html(directory: Path) -> str:
    req = tickets.read_artifact(directory, "requirement.md") or ""
    plan = _section(req, r"^##\s*3\.?\s*修改方案.*$")
    if not plan:
        return ""
    # 去掉 AI 指令样板行，只留产品真正写的需求内容
    kept = [ln for ln in plan.splitlines()
            if not any(noise in ln for noise in _PLAN_NOISE)]
    cleaned = "\n".join(kept).strip()
    return summary_md.render(_humanize.humanize(cleaned)) if cleaned else ""


def _points(directory: Path) -> list[dict]:
    td = directory / "test-design.json"
    if not td.exists():
        return []
    try:
        data = json.loads(td.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            if "mark" in n or "testPoint" in n:
                text = str(n.get("text", ""))
                pr = None
                mark = n.get("mark")
                if isinstance(mark, dict) and isinstance(mark.get("priority"), dict):
                    pr = next((k for k in ("1", "2", "3") if mark["priority"].get(k) is True), None)
                # 标题 平台_模块_要点 → 用 · 分隔，更易读
                label = " · ".join(p for p in text.split("_") if p)
                out.append({"label": label, "priority": pr})
            for c in n.get("children", []) or []:
                walk(c)

    walk(data)
    return out


def _platforms(points: list[dict], product: str) -> list[str]:
    seen = []
    labels = get_product(product).platform_labels
    for p in points:
        head = p["label"].split(" · ")[0].strip().lower()
        for key, lab in labels.items():
            if head == key:
                if lab not in seen:
                    seen.append(lab)
                break
    return seen


def build(directory: Path) -> dict:
    product = _product_from_dir(directory)
    points = _points(directory)
    badge = tickets.badge(directory)
    qpath = directory / "questions.md"
    pending = 0
    if qpath.exists():
        parsed = q_svc.parse(qpath)
        pending = parsed.get("counts", {}).get("pending", 0)
    return {
        "title": _title(directory),
        "platforms": _platforms(points, product),
        "plan_html": _plan_html(directory),
        "points": points,
        "point_count": len(points),
        "pending": pending,
        "badge": badge,
        "json_ready": badge.get("has_design") and badge.get("json_ok"),
        "ready": badge.get("has_design") and badge.get("json_ok") and not badge.get("review_needs_action"),
        "review_needs_action": badge.get("review_needs_action", False),
        "review_summary": badge.get("review_summary", ""),
    }


def spotcheck_html(directory: Path) -> str:
    raw = tickets.read_artifact(directory, "_spot-check.md") or ""
    legacy_zero = "已复核 0 条 · 确认 0 条建议" in raw and "已检查" not in raw
    if legacy_zero:
        raw = (
            "> ⚠️ 这是旧版复核记录：旧逻辑可能把“强模型未返回候选发现/解析失败被吞掉”的情况也显示成"
            "零条复核提示，不能证明本次复核已成功完成。请重新点击“复核用例”；新记录应显示"
            "`已检查 5/5 个复核维度`，若模型失败会明确列出失败原因。\n\n"
            + raw
        )
    # 旧产物（含原 Claude Code 工作流版）标题用语归一为「复核」
    for a, b in (("强模型抽检", "复核结果"), ("强抽检", "复核"), ("抽检", "复核")):
        raw = raw.replace(a, b)
    return summary_md.render(_humanize.clean_review(raw))
