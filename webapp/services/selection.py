"""选单/看板服务：包 select_sprint + 读 .sprint-state 文件 → 看板视图模型。"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import config
from . import questions as q_svc, scripts_loader, tickets

ROLE_CN = {"main": "主单", "split": "拆单", "standalone": "独立单"}

_SEL_LOCK = threading.Lock()  # 串行化 in-process 选单的 QA_TICKETS_ROOT env 设置，防多用户并发串目录


def _state_dir(product: str) -> Path:
    if not tickets.safe_product(product):
        return config.tickets_root() / "__invalid__" / ".sprint-state"
    return config.tickets_root() / product / ".sprint-state"


def read_selection(product: str, date: str) -> Optional[dict]:
    p = _state_dir(product) / f"_selection-{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def read_progress(product: str, date: str) -> dict:
    p = _state_dir(product) / f"_sprint-progress-{date}.json"
    if not p.exists():
        return {"done": []}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {"done": []}
    except Exception:  # noqa: BLE001
        return {"done": []}


def read_summary(product: str, date: str) -> Optional[str]:
    p = _state_dir(product) / f"_sprint-summary-{date}.md"
    return p.read_text(encoding="utf-8") if p.exists() else None


def read_spotcheck_summary(product: str, date: str) -> Optional[str]:
    p = _state_dir(product) / f"_spot-check-summary-{date}.md"
    return p.read_text(encoding="utf-8") if p.exists() else None


def load_ledger(product: str) -> dict:
    try:
        return scripts_loader.select_sprint().load_ledger(product)
    except Exception:  # noqa: BLE001
        return {"features": {}}


def run_selection(product: str, date: str, env: dict, board=None, tester: str | None = None) -> dict:
    """重新选单（只读 Jira，写 _selection-*.{md,json} 到【当前用户】工单根）。调用方需用 invoke() 包。

    in-process 调用 select_sprint：临时把 QA_TICKETS_ROOT 指到当前用户根（加锁串行，避免多用户
    并发互相串目录），让选单产物写进该用户独立目录。子进程生成另走 subprocess_env 注入、互不影响。
    """
    ss = scripts_loader.select_sprint()
    with _SEL_LOCK:
        prev = os.environ.get("QA_TICKETS_ROOT")
        os.environ["QA_TICKETS_ROOT"] = str(config.tickets_root())
        try:
            plan = ss.plan(product, date, board=board, env=env, tester=tester)
            ss.write_reports(plan)
        finally:
            if prev is None:
                os.environ.pop("QA_TICKETS_ROOT", None)
            else:
                os.environ["QA_TICKETS_ROOT"] = prev
    return plan


def list_jira_sprints(product: str, env: dict) -> list[dict]:
    """从 Jira 看板拉取 sprint 供用户选择新增。返回 [{name,state,date}]：
    进行中/未来在前、再按日期倒序；日期可在名字任意位置（含『预排期2026-06-16.BETA』这类未来）。
    调用方需用 invoke() 包（jira_fetch 用 SystemExit 报错）。"""
    import re
    ss = scripts_loader.select_sprint()
    jf = scripts_loader.jira_fetch()
    cfg = scripts_loader.load_env().load_raw_config()
    bid = ss._board_id(cfg, product, None)
    rank = {"ACTIVE": 0, "FUTURE": 1, "CLOSED": 2}
    out = []
    for s in jf.list_sprints(bid, env):
        name = str(s.get("name", ""))
        m = re.search(r"(\d{4}-\d{2}-\d{2})", name)  # 日期可在任意位置
        if not m:
            continue
        out.append({"name": name, "state": str(s.get("state", "")), "date": m.group(1)})
    # 同日期去重：保留状态优先级更高者（进行中>未来>已结束）
    best: dict[str, dict] = {}
    for s in out:
        cur = best.get(s["date"])
        if cur is None or rank.get(s["state"], 9) < rank.get(cur["state"], 9):
            best[s["date"]] = s
    uniq = sorted(best.values(), key=lambda x: (rank.get(x["state"], 9), x["date"]),
                  reverse=False)
    closed = sorted([s for s in uniq if s["state"] == "CLOSED"],
                    key=lambda x: x["date"], reverse=True)
    live = [s for s in uniq if s["state"] != "CLOSED"]
    live.sort(key=lambda x: (rank.get(x["state"], 9), x["date"]))
    return live + closed   # 全部返回（进行中/未来在前、已结束按日期倒序），不再截断为最近 12 个


def _has_design(product: str, key: str) -> bool:
    """该工单是否已生成用例（test-design.json 存在）——「已生成」状态/计数的唯一真源。"""
    td = tickets.find_ticket(product, key)
    return bool(td and (td / "test-design.json").exists())


def board(product: str, date: str) -> dict:
    """组装 sprint 看板视图模型（纯本地读 + in-process 校验徽章）。"""
    sel = read_selection(product, date)
    decisions = (sel or {}).get("decisions", [])
    run_list = (sel or {}).get("run_list", [])
    run_set = set(run_list)
    candidate = (sel or {}).get("candidate_count", len(decisions))
    coverage_size = (sel or {}).get("coverage_size", 0)

    rows = []
    for d in decisions:
        key = d.get("key")
        is_run = d.get("decision") == "run"
        row = {
            "key": key,
            "summary": d.get("summary", ""),
            "role": d.get("role", "main"),
            "role_cn": ROLE_CN.get(d.get("role", "main"), d.get("role", "")),
            "platform": d.get("platform"),
            "decision": d.get("decision"),
            "reason": d.get("reason", ""),
            "is_run": is_run,
        }
        if is_run:
            tdir = tickets.find_ticket(product, key)
            # 「待确认」统一口径 = questions.md 未回答的问题数（与工单说明/工单头一致）
            qp = (tdir / "questions.md") if tdir else None
            row["q_pending"] = (q_svc.parse(qp).get("counts", {}).get("pending", 0)
                                if qp and qp.exists() else 0)
            needs_resume = bool(tdir and q_svc.needs_resume(tdir))
            if tdir and (tdir / "test-design.json").exists():
                b = tickets.badge(tdir)
                row.update({
                    "has_design": True,
                    "has_questions": bool(qp and qp.exists()),
                    "needs_resume": needs_resume,
                    "json_ok": b["json_ok"],
                    "json_fail": b["json_fail"],
                    "json_warn": b["json_warn"],
                    "content_ok": b["content_ok"],
                    "content_rc": b["content_rc"],
                    "testpoints": b["testpoints"],
                    "todo": b["todo"],
                    "review_needs_action": b.get("review_needs_action", False),
                    "review_summary": b.get("review_summary", ""),
                    "date_found": tdir.parent.name,
                    "status": "pending" if needs_resume else "done",
                })
            else:
                has_q = bool(tdir and (tdir / "questions.md").exists())
                row.update({"has_design": False, "has_questions": has_q,
                            "needs_resume": needs_resume,
                            "status": "pending", "date_found": date})
        rows.append(row)

    total = len(run_list)
    # 「已生成」以产物为唯一真源（test-design.json 存在）= has_design 行数，与列表行状态、首页卡片一致
    done_in_run = sum(1 for r in rows if r.get("is_run") and r.get("has_design"))
    pct = round(done_in_run / total * 100) if total else 0
    # 已有待确认答案需要推动后续生成：包括草稿阶段（无正式用例）和复核后新增确认（已有正式用例）。
    awaiting_continue = len([r for r in rows if r.get("is_run") and r.get("needs_resume")])

    return {
        "product": product,
        "date": date,
        "exists": sel is not None,
        "selection": sel or {},
        "jql_total": (sel or {}).get("jql_total"),
        "jql": (sel or {}).get("jql"),
        "candidate": candidate,
        "run_count": total,
        "skip_count": max(candidate - total, 0),
        "coverage_size": coverage_size,
        "review_needed": (sel or {}).get("review_needed", []),
        "sprint_meta": (sel or {}).get("sprint_meta", []),
        "generated": (sel or {}).get("generated"),
        "done_count": done_in_run,
        "pct": pct,
        "pending": max(total - done_in_run, 0),   # 待生成（剩余）= 运行清单 − 已生成；全做完即 0
        "awaiting_continue": awaiting_continue,
        "rows": rows,
        "has_summary": read_summary(product, date) is not None,
        "has_spotcheck": read_spotcheck_summary(product, date) is not None,
    }


def sprint_dates(product: str) -> list[str]:
    """当前用户工单根下的全部 Sprint 日期（倒序）= 已生成目录 ∪ 已同步选单。
    数据天然按用户隔离（tickets_root 已是该用户的独立根），无需归属过滤。"""
    dates = set(tickets.sprint_dates(product))
    sd = _state_dir(product)
    if sd.exists():
        for p in sd.glob("_selection-*.json"):
            m = re.match(r"_selection-(\d{4}-\d{2}-\d{2})\.json$", p.name)
            if m:
                dates.add(m.group(1))
    return sorted(dates, reverse=True)


def overview(product: str, user: str = "") -> dict:
    """首页：当前用户工单根下的 Sprint 概览（数据天然按用户隔离，无需归属过滤）。"""
    out = []
    for date in sprint_dates(product):
        sel = read_selection(product, date)
        run_list = (sel or {}).get("run_list", [])
        out.append({
            "date": date,
            "has_selection": sel is not None,
            "run_count": len(run_list),
            "done_count": sum(1 for k in run_list if _has_design(product, k)),
            "candidate": (sel or {}).get("candidate_count"),
            "ticket_count": len(tickets.list_ticket_dirs(product, date)),
        })
    return {"product": product, "sprints": out}


def delete_sprint(product: str, date: str) -> dict:
    """删除一个 Sprint（供 webapp 调用；调用方须先校验归属 + 非忙）。可恢复优先：

    - 工单产物 tickets/<product>/<date>/ → 移入 tickets/<product>/.trash/<date>-<时间戳>/（移动而非硬删）
    - 清理 .sprint-state 下该日期的选单 / 进度 / 汇总 / 复核汇总文件
    - 覆盖账本 coverage-ledger.json 移除「由本 sprint 覆盖」的特性（使其将来可重新选中）
    - 移除归属账本条目

    返回 {trashed, tickets, state_files, ledger_removed}。
    """
    if not (tickets.safe_product(product) and tickets.safe_date(date)):
        raise ValueError("产品或 Sprint 日期有误")
    base = config.tickets_root() / product
    info = {"trashed": None, "tickets": 0, "state_files": 0, "ledger_removed": 0}

    # 1) 工单产物 → 回收站（移动，可恢复；绝不直接删核心交付物 test-design.json）
    sprint_dir = base / date
    if sprint_dir.exists() and sprint_dir.is_dir():
        info["tickets"] = len(tickets.list_ticket_dirs(product, date))
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        trash = base / ".trash" / f"{date}-{ts}"
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sprint_dir), str(trash))
        info["trashed"] = str(trash)

    # 2) 运行态文件（选单/进度/汇总——可由「同步 Sprint」再生成，直接删）
    sd = base / ".sprint-state"
    for name in (f"_selection-{date}.json", f"_selection-{date}.md",
                 f"_sprint-progress-{date}.json", f"_sprint-summary-{date}.md",
                 f"_spot-check-summary-{date}.md"):
        p = sd / name
        if p.exists():
            try:
                p.unlink()
                info["state_files"] += 1
            except OSError:
                pass

    # 3) 覆盖账本：移除由本 sprint 覆盖的特性（否则其将来在其它 sprint 被永久跳过，而产物已移走）
    led_path = sd / "coverage-ledger.json"
    if led_path.exists():
        try:
            led = json.loads(led_path.read_text(encoding="utf-8")) or {}
            feats = led.get("features") or {}
            removed = [f for f, m in feats.items() if (m or {}).get("sprint") == date]
            for f in removed:
                feats.pop(f, None)
            if removed:
                tmp = led_path.with_name(led_path.name + ".tmp")
                tmp.write_text(json.dumps(led, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, led_path)  # 原子写
                info["ledger_removed"] = len(removed)
        except Exception:  # noqa: BLE001
            pass

    return info
