"""工单服务：目录发现、产物读取、校验徽章（按 mtime 缓存）。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .. import config
from . import artifacts, scripts_loader
from core.productcfg import DEFAULT_PRODUCT, get_product, valid_product_key
from core.store.db import session_scope
from core.store.repositories import ArtifactRepository

# 路径段白名单：防越权/路径穿越（登录用户也防 fat-finger 与构造的 ".." 段）
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INVALID = "__invalid__"  # 指向 tickets/ 内必不存在的段，既不逃逸也让 exists()=False


def safe_product(p: str) -> bool:
    return valid_product_key(p or "")


def safe_date(d: str) -> bool:
    return bool(_DATE_RE.match(d or ""))


def safe_ear(e: str, product: str = DEFAULT_PRODUCT) -> bool:
    return get_product(product).valid_ticket_key(e or "")

# 工单详情页 Tab 顺序（存在才显示）
ARTIFACT_TABS = [
    ("requirement", "需求", "requirement.md"),
    ("analysis", "分析", "analysis.md"),
    ("business-context", "业务上下文", "business-context.md"),
    ("linked-issues", "关联工单", "linked-issues.md"),
    ("test-points", "测试点", "test-points.md"),
    ("packet", "QA 包", "_qa-packet.md"),
]

_BADGE_CACHE: dict[str, tuple[float, dict]] = {}


def _owner_from_root() -> str | None:
    try:
        rel = config.tickets_root().resolve().relative_to(config.USERDATA_DIR.resolve())
    except ValueError:
        return "" if config.tickets_root() == config.TICKETS_DIR else None
    return rel.parts[0] if len(rel.parts) >= 2 and rel.parts[1] == "tickets" else None


def _db_sprint_dates(product: str) -> list[str]:
    owner = _owner_from_root()
    engine = config.platform_engine()
    if owner is None or engine is None:
        return []
    try:
        with session_scope(engine) as session:
            return ArtifactRepository(session).list_sprints(product_key=product, owner_username=owner)
    except Exception:
        return []


def _materialize_from_db(product: str, date: str, ear: str) -> Path | None:
    owner = _owner_from_root()
    if owner is None:
        return None
    return artifacts.materialize_ticket(
        owner_username=owner,
        product=product,
        sprint=date,
        ticket_key=ear,
        root=config.tickets_root().parent,
    )


def sprint_dates(product: str) -> list[str]:
    """tickets/<product>/ 下形如 YYYY-MM-DD 的 sprint 目录，按日期倒序。"""
    if not safe_product(product):
        return []
    base = config.tickets_root() / product
    out = set(_db_sprint_dates(product))
    if base.exists():
        for p in base.iterdir():
            if p.is_dir() and len(p.name) == 10 and p.name[4] == "-" and p.name[:4].isdigit():
                out.add(p.name)
    return sorted(out, reverse=True)


def ticket_dir(product: str, date: str, ear: str) -> Path:
    if not (safe_product(product) and safe_date(date) and safe_ear(ear, product)):
        return config.tickets_root() / _INVALID  # 畸形输入：必不存在且不逃逸 tickets/
    path = config.tickets_root() / product / date / ear
    if not path.exists():
        materialized = _materialize_from_db(product, date, ear)
        if materialized is not None:
            return materialized
    return path


def find_ticket(product: str, ear: str) -> Optional[Path]:
    if not (safe_product(product) and safe_ear(ear, product)):
        return None
    hits = list((config.tickets_root() / product).glob(f"*/{ear}"))
    if hits:
        return hits[0]
    owner = _owner_from_root()
    engine = config.platform_engine()
    if owner is None or engine is None:
        return None
    try:
        with session_scope(engine) as session:
            ticket = ArtifactRepository(session).find_ticket(product_key=product, ticket_key=ear, owner_username=owner)
            sprint = ticket.sprint if ticket is not None else ""
        if not sprint:
            return None
        return _materialize_from_db(product, sprint, ear)
    except Exception:
        return None


def list_ticket_dirs(product: str, date: str) -> list[Path]:
    base = config.tickets_root() / product / date
    pc = get_product(product)
    paths = set()
    if base.exists():
        paths.update(p for p in base.glob(pc.ticket_glob()) if p.is_dir() and pc.valid_ticket_key(p.name))
    owner = _owner_from_root()
    engine = config.platform_engine()
    if owner is not None and engine is not None:
        try:
            with session_scope(engine) as session:
                db_tickets = ArtifactRepository(session).list_tickets(product_key=product, sprint=date, owner_username=owner)
                keys = [str(ticket.key) for ticket in db_tickets]
            for key in keys:
                if pc.valid_ticket_key(key):
                    materialized = _materialize_from_db(product, date, key)
                    if materialized is not None:
                        paths.add(materialized)
        except Exception:
            pass
    return sorted(paths)


def read_artifact(directory: Path, name: str) -> Optional[str]:
    p = directory / name
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return f"(读取失败：{e})"


def _signature(directory: Path) -> float:
    """目录关键文件的最大 mtime，作为徽章缓存键。"""
    sig = 0.0
    for name in ("test-design.json", "questions.md", "analysis.md", "requirement.md", "_spot-check.md", "_revise.md"):
        p = directory / name
        if p.exists():
            try:
                sig = max(sig, p.stat().st_mtime)
            except OSError:
                pass
    return sig


def json_structure_issues(directory: Path) -> list:
    """in-process 跑 validate-test-design 的 Validator（快，无子进程）。"""
    td = directory / "test-design.json"
    if not td.exists():
        return []
    vtd = scripts_loader.validate_test_design()
    try:
        return vtd.Validator(td).validate()
    except Exception:  # noqa: BLE001
        return []


def _stats(directory: Path) -> dict:
    td = directory / "test-design.json"
    if not td.exists():
        return {"testpoints": 0, "steps": 0, "expects": 0, "todo": 0}
    try:
        bg = scripts_loader.batch_generate()
        return bg.count_stats(json.loads(td.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return {"testpoints": 0, "steps": 0, "expects": 0, "todo": 0}


def _content_check(directory: Path) -> tuple[int, str]:
    """内容契约校验（check-ticket-artifacts.py，子进程；含 questions 稳定格式）。"""
    bg = scripts_loader.batch_generate()
    try:
        return bg.run_validator("check-ticket-artifacts.py", str(directory))
    except Exception as e:  # noqa: BLE001
        return 2, f"运行校验器失败：{e}"


def review_status(directory: Path) -> dict:
    """确定性解析复核/修复闭环状态；只读报告中的明确标记，不做语义猜测。"""
    spot = read_artifact(directory, "_spot-check.md") or ""
    revise = read_artifact(directory, "_revise.md") or ""
    text = f"{spot}\n{revise}"
    has_spotcheck = bool(spot)
    failed = any(x in text for x in ("本次复核失败", "复核未完整完成", "修后验收失败"))
    unfixable = any(x in text for x in ("需重新生成或人工补", "需重新生成 / 人工补"))
    leftover = any(x in text for x in ("修后验收仍需处理", "修后验收未消解", "仍发现未消解项"))
    skipped_questions = "以下复核建议未转成待确认" in text
    pending_questions = "需你确认（已转成" in text
    confirmed_match = re.search(r"确认\s*(\d+)\s*条建议", spot)
    confirmed = int(confirmed_match.group(1)) if confirmed_match else 0
    applied_match = re.search(r"已应用\s*(\d+)\s*处节点修订", revise or spot)
    applied = int(applied_match.group(1)) if applied_match else 0
    needs_action = failed or unfixable or leftover or skipped_questions or pending_questions
    if failed:
        summary = "复核/修后验收失败，请重试复核或检查模型配置。"
    elif leftover:
        summary = "自动修复后仍有复核建议未消解，需要人工处理或重新复核。"
    elif unfixable:
        summary = "复核发现需重新生成或人工补的结构性问题。"
    elif skipped_questions:
        summary = "有复核待确认因依据不足未写入待确认，请重新复核。"
    elif pending_questions:
        summary = "复核新增了待确认问题，回答后需要继续生成。"
    elif has_spotcheck and confirmed:
        summary = "复核建议已修复或正确分流。"
    elif has_spotcheck:
        summary = "复核未发现需修正的问题。"
    else:
        summary = "尚未复核。"
    return {
        "has_spotcheck": has_spotcheck,
        "needs_action": needs_action,
        "failed": failed,
        "unfixable": unfixable,
        "leftover": leftover,
        "skipped_questions": skipped_questions,
        "pending_questions": pending_questions,
        "confirmed": confirmed,
        "applied": applied,
        "summary": summary,
    }


def badge(directory: Path) -> dict:
    """工单状态徽章（缓存）：JSON 结构 / 内容契约 / 测试点 / 待确认 / 是否有产物。"""
    key = str(directory)
    sig = _signature(directory)
    cached = _BADGE_CACHE.get(key)
    if cached and cached[0] == sig:
        return cached[1]

    has_design = (directory / "test-design.json").exists()
    issues = json_structure_issues(directory) if has_design else []
    json_fail = sum(1 for i in issues if i.level == "FAIL")
    json_warn = sum(1 for i in issues if i.level == "WARN")
    st = _stats(directory)
    review = review_status(directory)
    # 「用例校验」只认 test-design.json 本身（validate-test-design 已覆盖结构/平台/UUID/marker/自包含
    # [来源:]·§·损坏 HTML）。不再跑 check-ticket-artifacts——它查 README/requirement/analysis 等中间产物
    # 格式（多个一级标题、缺文件等），与用例交付物无关；之前会因隐藏中间文件把好用例误标「有问题」。
    # 顺带省掉每单一次子进程，看板更快。content_ok/content_rc 保留为中性值（已无界面消费）。
    rc = 0 if has_design else -1
    b = {
        "has_design": has_design,
        "json_ok": has_design and json_fail == 0,
        "json_fail": json_fail,
        "json_warn": json_warn,
        "content_ok": rc == 0,
        "content_rc": rc,
        "testpoints": st.get("testpoints", 0),
        "todo": st.get("todo", 0),
        "has_spotcheck": (directory / "_spot-check.md").exists(),
        "has_questions": (directory / "questions.md").exists(),
        "review_status": review,
        "review_needs_action": review["needs_action"],
        "review_failed": review["failed"],
        "review_unfixable": review["unfixable"],
        "review_leftover": review["leftover"],
        "review_summary": review["summary"],
    }
    _BADGE_CACHE[key] = (sig, b)
    return b


def invalidate_badge(directory: Path) -> None:
    _BADGE_CACHE.pop(str(directory), None)
