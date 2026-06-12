#!/usr/bin/env python
"""
scripts/select_sprint.py

Sprint 工单【选取器】——把"这个 sprint 哪些工单该走流程"从手工挑单变成确定性规则。
只读 Jira（POST 搜索）+ 只读本地产物；本身**不生成任何用例**（不耗弱模型额度），
只产出"运行清单 + 决策报告"。实际驱动由 run_sprint.py --select 或 /qa:sprint 完成。

选单规则（与用户确认，固化于此，勿再口头强调）：
  1. 候选 = JQL(project ∈ 配置 ∧ resolution = Unresolved) ∩ sprint(指定日期)
     再【客户端过滤】issuetype = 提高 ∧ 问题测试员(customfield_10020) = 林子宣。
     （issuetype/中文字段不能进 JQL：Jira 拒中文 issuetype 值、网关拦 GET 中文——见 jira_fetch.search）
  2. 去重单元 = "功能特性"，以【主单】标识。每个特性最终只产【一份】产物。
     - 拆单识别：标题形如 `<主单标题>--<平台后缀>`（如 --新web/--app/--接口），分隔符 `--`。
     - 找主单：在拆单的关联工单里，标题正好等于"去掉后缀"的那个即主单
       （此实例拆单用通用 `related` 关联，**不是**专用"拆分"链接，故只能靠标题匹配）。
  3. 决策（按特性分组）：
     - 特性【已走过】（账本记录 or 已有目录含通过校验的 test-design.json）→ 全部跳过。
     - 否则未走过：主单在本 sprint → 跑【主单】，跳过同组拆单；
                   主单缺席（只有拆单）→ 只跑【一个拆单】（按平台优先级），跑时读主单需求。
     - 跑完记账（由 run_sprint 成功后写）：该特性已覆盖 ⇒ 将来轮到主单所在 sprint 自动跳过。
       （"主单在之前/之后 sprint"两种情况都被"是否已覆盖"统一处理，无需比较 sprint 先后）

用法：
  python scripts/select_sprint.py --product wms --sprint 2026-06-02            # 选单 + 报告（只读）
  python scripts/select_sprint.py --product wms --sprint 2026-06-02 --board 236
  python scripts/select_sprint.py --product wms --sprint 2026-06-02 --keys-only # 仅打印逗号分隔运行清单
作为库（被 run_sprint 调用）：
  from select_sprint import plan, write_reports, mark_covered
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent


def _tickets_root() -> Path:
    """工单根目录：调用时读 QA_TICKETS_ROOT（webapp 按用户注入；in-process 临时设）；默认仓库 tickets/。
    本脚本既被子进程调用又被 webapp in-process 调用，故必须调用时解析、不能用 import 期常量。"""
    return Path(os.environ.get("QA_TICKETS_ROOT") or (REPO_ROOT / "tickets"))


if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import jira_fetch  # noqa: E402
import batch_generate as bg  # noqa: E402  (复用 run_validator)
import _load_env  # noqa: E402
from core.productcfg import DEFAULT_PRODUCT, get_product  # noqa: E402

# 选单只需轻量字段（不取 description/comment，翻页才快）
SELECT_FIELDS = ("summary,issuetype,status,resolution,"
                 "customfield_10020,customfield_10125,issuelinks")

# 默认选单参数（config 的 selection 段可覆盖）
DEFAULTS = {
    "tester": "林子宣",
    # 测试员进 JQL 的字段引用（实测 POST body 里中文不触发网关 WAF，cf[10020] 可用）。
    # 设为 "" 则不进 JQL、只客户端过滤。配错会导致 JQL 命中 0——看报告"JQL 命中"行即可发现。
    "tester_jql_field": "cf[10020]",
    "issuetype": "提高",
    "resolution": "Unresolved",          # 兼容旧用法；新口径用 resolutions_allowed 客户端过滤
    # 一个用户在一个 sprint 的全部需求工单 = 未解决(resolution 为空) ∪ 已修复；不判状态。
    # 中文 resolution 进 JQL 会被 Jira 拒（同 issuetype），故 JQL 不过滤 resolution、客户端过滤。
    "resolutions_allowed": ["已修复", "Fixed"],  # 这些 + 空(未解决) 才入选
    # 拆单后缀分隔符 `--` + 末尾短平台串（≤12 字符、无连字符）。base 贪婪取到最后一个 `--`。
    "split_regex": r"^(?P<base>.+)--(?P<suffix>[^-]{1,12})$",
    # 主单缺席时挑哪个拆单（按平台优先级，命中靠前者优先；都不命中按工单号升序）
    "split_pick_priority": ["web", "app", "接口", "pc", "小程序", "h5", "pos"],
}


# ----------------------------- 配置 ---------------------------------------

def _sel_cfg() -> dict:
    cfg = _load_env.load_raw_config()
    sel = dict(DEFAULTS)
    sel.update((cfg.get("selection") or {}))
    # webapp 多用户：问题测试员按当前用户传入（in-process 走 plan(tester=)，子进程走 QA_SELECT_TESTER）
    t = os.environ.get("QA_SELECT_TESTER")
    if t:
        sel["tester"] = t
    return cfg, sel


def _product_cfg(cfg: dict, product: str) -> dict:
    p = ((cfg.get("products") or {}).get(product)) or {}
    return p


def _ticket_design_paths(product: str):
    pc = get_product(product)
    for td in (_tickets_root() / product).glob(f"*/{pc.ticket_glob()}/test-design.json"):
        if pc.valid_ticket_key(td.parent.name):
            yield td


def _jira_raw_paths(product: str):
    pc = get_product(product)
    for raw in (_tickets_root() / product).glob(f"*/{pc.ticket_glob()}/_jira-raw.json"):
        if pc.valid_ticket_key(raw.parent.name):
            yield raw


def _derive_board_id(product: str) -> int | None:
    """config 未配 jira_board_id 时，从本地 _jira-raw.json 的 sprint 字段里抠 rapidViewId 兜底。"""
    for raw in _jira_raw_paths(product):
        try:
            d = json.loads(raw.read_text(encoding="utf-8"))
        except Exception:
            continue
        sp = (d.get("fields") or {}).get("customfield_10125")
        text = " ".join(sp) if isinstance(sp, list) else str(sp)
        m = re.search(r"rapidViewId=(\d+)", text)
        if m:
            return int(m.group(1))
    return None


def _board_id(cfg: dict, product: str, override) -> int:
    if override:
        return int(override)
    pc = _product_cfg(cfg, product)
    if pc.get("jira_board_id") is not None:
        return int(pc["jira_board_id"])
    bid = _derive_board_id(product)
    if bid is not None:
        return bid
    raise SystemExit(
        f"[select_sprint] 未找到产品 {product} 的看板 id：请在 config.local.yaml 的 "
        f"`products.{product}.jira_board_id` 填写（WMS 实测为 236），或用 --board 指定。")


def _project_keys(cfg: dict, product: str) -> list[str]:
    pc = _product_cfg(cfg, product)
    keys = pc.get("jira_project_keys") or (cfg.get("jira") or {}).get("default_project_keys") or ["EAR"]
    return [str(k) for k in keys]


# ----------------------------- 工具 ---------------------------------------

def _norm(s: str) -> str:
    """标题归一：去首尾空白 + 全角空格折叠，便于拆单 base 与主单标题精确比对。"""
    return re.sub(r"\s+", "", (s or "").strip())


def _earnum(key: str) -> int:
    m = re.sub(r"\D", "", key or "")
    return int(m) if m else 0


def _tester_names(field) -> list[str]:
    if field is None:
        return []
    if isinstance(field, list):
        out = []
        for x in field:
            out += _tester_names(x)
        return out
    if isinstance(field, dict):
        v = field.get("displayName") or field.get("name") or field.get("value")
        return [v] if v else []
    return [str(field)]


def _linked(issue: dict) -> list[tuple[str, str]]:
    """返回该工单所有关联工单的 (key, summary)。关系类型不限——主单/拆单靠标题判定。"""
    out = []
    for l in (issue.get("fields") or {}).get("issuelinks") or []:
        oi = l.get("outwardIssue") or l.get("inwardIssue")
        if not oi:
            continue
        out.append((oi.get("key"), ((oi.get("fields") or {}).get("summary")) or ""))
    return out


# --------------------------- 拆单/主单分类 ---------------------------------

class _Cache:
    def __init__(self, env):
        self.env = env
        self.full: dict[str, dict] = {}

    def get_full(self, key: str) -> dict:
        if key not in self.full:
            self.full[key] = jira_fetch.get_issue(key, self.env, fields=SELECT_FIELDS, expand="")
        return self.full[key]


def classify(issue: dict, sel: dict, cache: _Cache) -> dict:
    """判定一个工单是 主单 / 拆单 / 独立单（未匹配到主单），并算出其"功能特性"key。
    返回 {key, summary, role, feature, platform, note}。"""
    f = issue.get("fields") or {}
    key = issue.get("key")
    summary = f.get("summary") or ""
    m = re.match(sel["split_regex"], summary)
    if not m:
        return {"key": key, "summary": summary, "role": "main", "feature": key, "platform": None, "note": ""}

    base = _norm(m.group("base"))
    suffix = m.group("suffix").strip()
    # 关联工单标题匹配主单——search 结果的 issuelinks 可能不带 summary，缺则拉全单补
    links = _linked(issue)
    if not any(t for _, t in links):
        links = _linked(cache.get_full(key))
    exact = [(k, t) for k, t in links if _norm(t) == base]
    if exact:
        return {"key": key, "summary": summary, "role": "split", "feature": exact[0][0],
                "platform": suffix, "note": f"主单 {exact[0][0]}"}
    # 精确匹配不到主单：**不做前缀"容错"**——本实例拆单走通用 related「相关联的问题」，关联列表常
    # 混入无关单（缺陷/汇总单，如 242290 挂着无关的 249237），前缀匹配会把"<主单标题>…"开头的无关单
    # 误判成主单（假阳）。宁可落独立单走人工复核（§4.5.4 安全网），不静默错挂。
    return {"key": key, "summary": summary, "role": "standalone", "feature": key, "platform": suffix,
            "note": "检出 --后缀 但未在关联工单里找到【标题正好等于去后缀前缀】的主单 → 按独立单处理，请人工复核"}


# ----------------------------- 候选拉取 -----------------------------------

def fetch_candidates(product: str, date: str, board, env: dict, cfg: dict, sel: dict) -> dict:
    bid = _board_id(cfg, product, board)
    ids, meta = jira_fetch.resolve_sprint_ids(date, bid, env)
    if not ids:
        names = sorted({str(s.get("name")) for s in jira_fetch.list_sprints(bid, env)
                        if str(s.get("name", "")).startswith(date[:7])})
        raise SystemExit(
            f"[select_sprint] 看板 {bid} 下未找到 name 以 {date} 开头的 sprint。\n"
            f"  同月可选：{names or '（无）'}\n  （sprint 目录名取日期前缀，sprint 实际 name 可能带 .BETA 等后缀）")
    keys = _project_keys(cfg, product)
    tester_clause = ""
    if sel.get("tester_jql_field") and sel.get("tester"):
        # 测试员进 JQL（POST body 中文安全），把全实例未解决单收窄到该测试员
        tester_clause = f' AND {sel["tester_jql_field"]} = "{sel["tester"]}"'
    # 不在 JQL 过滤 resolution（中文值会被 Jira 拒）；拉该测试员本 sprint 全部单，再客户端按口径过滤
    jql = (f"project in ({','.join(keys)}){tester_clause} "
           f"AND sprint in ({','.join(str(i) for i in ids)}) ORDER BY key DESC")
    raw = jira_fetch.search(jql, SELECT_FIELDS, env)
    allowed_res = set(sel.get("resolutions_allowed") or ["已修复", "Fixed"])
    # 客户端过滤：issuetype（中文不能进 JQL）+ 问题测试员（兜底）+ 解决结果(未解决∪已修复)
    kept = []
    for it in raw:
        f = it.get("fields") or {}
        if (f.get("issuetype") or {}).get("name") != sel["issuetype"]:
            continue
        if sel["tester"] not in _tester_names(f.get("customfield_10020")):
            continue
        res_name = (f.get("resolution") or {}).get("name")
        if not (res_name is None or res_name in allowed_res):  # 未解决(None) 或 已修复
            continue
        kept.append(it)
    return {"candidates": kept, "jql": jql, "sprint_meta": meta, "board_id": bid,
            "raw_total": len(raw)}


# ----------------------------- 覆盖（账本） --------------------------------

def _state_dir(product: str) -> Path:
    d = _tickets_root() /product / ".sprint-state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ledger_path(product: str) -> Path:
    return _state_dir(product) / "coverage-ledger.json"


def load_ledger(product: str) -> dict:
    p = _ledger_path(product)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            sys.stderr.write(f"[select_sprint] ⚠️ 账本损坏，忽略既有内容：{p}\n")
    return {"features": {}}


def _save_ledger(product: str, led: dict) -> None:
    p = _ledger_path(product)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(led, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)  # 原子写，防中断截断


def mark_covered(product: str, feature: str, covered_by: str, sprint: str,
                 platform: str | None = None, source: str = "run") -> None:
    """登记某功能特性已产出（由 run_sprint 成功后调用）。已存在则不覆盖首登记。"""
    led = load_ledger(product)
    feats = led.setdefault("features", {})
    if feature not in feats:
        feats[feature] = {"covered_by": covered_by, "sprint": sprint,
                          "platform": platform, "source": source,
                          "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        _save_ledger(product, led)


def _validate_pass(td: Path) -> bool:
    if not (td.exists() and td.stat().st_size > 5):
        return False
    rc, _ = bg.run_validator("validate-test-design.py", str(td))
    return rc == 0


def build_coverage(product: str, env: dict, sel: dict, cache: _Cache, target_date: str) -> dict:
    """合并"账本 + 扫描已有目录"得到 已覆盖特性 → 元信息。
    **只统计目标 sprint 以外的 sprint**——跨 sprint 去重才是覆盖的本意；
    目标 sprint 自身的重跑由 run_sprint/qa_pipeline 的续跑(产物已在则跳过)/--force 处理，
    不能因目标 sprint 里已存在(可能是旧 agent 生成、未评审的)产物就把当前要处理的单判成"已走过"。
    扫描：tickets/<product>/<其他sprint>/<产品工单glob>/ 中 test-design.json 通过结构校验者，
    解析其特性（主单=自身；拆单→其主单），回填账本（source=scan，不覆盖已有）。"""
    led = load_ledger(product)
    feats = led.setdefault("features", {})
    changed = False
    scan_hits: dict[str, dict] = {}        # 本次扫描命中（循环开头已跳过目标 sprint，故必为其他 sprint）
    for td in _ticket_design_paths(product):
        sprint = td.parent.parent.name
        if sprint == target_date:                 # 目标 sprint 自身不计入覆盖
            continue
        if not _validate_pass(td):
            continue
        ear = td.parent.name
        # 解析该已产出单的特性：优先本地 _jira-raw.json，无则拉单
        raw = td.parent / "_jira-raw.json"
        issue = None
        if raw.exists():
            try:
                issue = json.loads(raw.read_text(encoding="utf-8"))
            except Exception:
                issue = None
        if issue is None:
            try:
                issue = cache.get_full(ear)
            except SystemExit:
                issue = {"key": ear, "fields": {"summary": ear}}
        info = classify(issue, sel, cache)
        feat = info["feature"]
        meta = {"covered_by": ear, "sprint": sprint, "platform": info.get("platform"),
                "source": "scan", "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        scan_hits[feat] = meta
        if feat not in feats:                     # 回填账本（首登记为准）
            feats[feat] = meta
            changed = True
    if changed:
        _save_ledger(product, led)
    # 决策用覆盖集 = 账本(排除目标 sprint 记录) ∪ 本次扫描命中(必为其他 sprint)。
    # union 关键：保证"其他 sprint 已有通过校验产物"一定算覆盖——即使账本里有本 sprint 的陈旧同特性
    # 记录占了 key（上一轮跑本 sprint 时记的），也不会因此漏掉真正的跨 sprint 覆盖事实。
    decision_cov = {f: m for f, m in feats.items() if m.get("sprint") != target_date}
    decision_cov.update(scan_hits)
    return decision_cov


# ----------------------------- 决策 ---------------------------------------

def _pick_split(members: list[dict], sel: dict) -> dict:
    prio = [p.lower() for p in sel["split_pick_priority"]]

    def rank(m):
        plat = (m.get("platform") or "").lower()
        pi = next((i for i, p in enumerate(prio) if p and p in plat), len(prio))
        return (pi, _earnum(m["key"]))

    return sorted(members, key=rank)[0]


def plan(product: str, date: str, board=None, env: dict | None = None,
         tester: str | None = None) -> dict:
    cfg, sel = _sel_cfg()
    if tester:                      # webapp 传入当前用户的问题测试员名（按用户过滤）
        sel = {**sel, "tester": tester}
    env = env or _load_env.parse_config()
    cache = _Cache(env)

    fc = fetch_candidates(product, date, board, env, cfg, sel)
    candidates = fc["candidates"]

    # 分类候选
    classified = [classify(it, sel, cache) for it in candidates]

    # 覆盖（账本 + 扫描其他 sprint 已有目录）
    coverage = build_coverage(product, env, sel, cache, date)

    # 按特性分组
    groups: dict[str, list[dict]] = {}
    for c in classified:
        groups.setdefault(c["feature"], []).append(c)

    decisions: list[dict] = []
    run_list: list[str] = []
    feature_of: dict[str, str] = {}
    review_needed: list[dict] = []

    for feature, members in groups.items():
        cov = coverage.get(feature)
        if cov:
            for m in members:
                decisions.append({**m, "decision": "skip",
                                  "reason": f"已走过：特性由 {cov['covered_by']} @ {cov['sprint']} 覆盖（{cov.get('source')}）"})
            continue
        mains_here = [m for m in members if m["role"] == "main" and m["feature"] == m["key"]]
        if mains_here:
            chosen = mains_here[0]
            chosen_reason = "主单优先（同组拆单跳过；主单方案覆盖全平台）"
        else:
            chosen = _pick_split(members, sel)
            sibs = [m["key"] for m in members if m["key"] != chosen["key"]]
            chosen_reason = ("主单缺席：代跑此拆单（跑时读主单需求规则）"
                             + (f"；同组其他拆单 {sibs} 跳过（需求相同）" if sibs else ""))
        run_list.append(chosen["key"])
        feature_of[chosen["key"]] = feature
        for m in members:
            if m["key"] == chosen["key"]:
                decisions.append({**m, "decision": "run", "reason": chosen_reason})
            else:
                decisions.append({**m, "decision": "skip",
                                  "reason": f"同特性已选 {chosen['key']} 代表，跳过"})
        if chosen["role"] == "standalone":
            review_needed.append(chosen)

    # 稳定排序：运行清单按工单号
    run_list = sorted(set(run_list), key=_earnum)
    decisions.sort(key=lambda d: (_earnum(d["key"])))
    return {
        "product": product, "sprint": date, "jql": fc["jql"],
        "sprint_meta": fc["sprint_meta"], "board_id": fc["board_id"],
        "jql_total": fc["raw_total"], "candidate_count": len(candidates), "run_list": run_list,
        "feature_of": feature_of, "decisions": decisions, "review_needed": review_needed,
        "coverage_size": len(coverage),
    }


# ----------------------------- 报告 ---------------------------------------

def _report_md(p: dict) -> str:
    meta = "; ".join(f"{s.get('name')}(id={s.get('id')},{s.get('state')})" for s in p["sprint_meta"])
    runs = [d for d in p["decisions"] if d["decision"] == "run"]
    skips = [d for d in p["decisions"] if d["decision"] == "skip"]
    role_cn = {"main": "主单", "split": "拆单", "standalone": "独立单(未匹配主单)"}
    L = [
        f"# Sprint 选单 · {p['product']} · {p['sprint']}", "",
        f"- 看板 id：{p['board_id']}　匹配 sprint：{meta}",
        f"- JQL：`{p['jql']}`",
        f"- JQL 命中（客户端过滤前）：{p.get('jql_total', '?')} 单",
        f"- 候选（问题测试员=林子宣 ∧ 类型=提高 ∧ 解决结果=未解决）：**{p['candidate_count']}** 单",
        f"- 账本已覆盖特性（其他 sprint）：{p['coverage_size']} 个", "",
        f"## ✅ 将运行（run_list，{len(p['run_list'])} 单）", "",
        "| 工单 | 标题 | 角色 | 功能特性(主单) | 原因 |", "|---|---|---|---|---|",
    ]
    for d in runs:
        plat = f"·{d['platform']}" if d.get("platform") else ""
        L.append(f"| {d['key']} | {d['summary']} | {role_cn.get(d['role'], d['role'])}{plat} | {d['feature']} | {d['reason']} |")
    L += ["", f"## ⏭️ 跳过（{len(skips)} 单）", "",
          "| 工单 | 标题 | 角色 | 原因 |", "|---|---|---|---|"]
    for d in skips:
        L.append(f"| {d['key']} | {d['summary']} | {role_cn.get(d['role'], d['role'])} | {d['reason']} |")
    if p["review_needed"]:
        L += ["", "## ⚠️ 需人工复核（检出拆单后缀但未匹配到主单）", ""]
        for d in p["review_needed"]:
            L.append(f"- {d['key']}：{d['summary']}　—— {d['note']}")
    L += ["", "---",
          f"> 驱动运行：`python scripts/run_sprint.py --product {p['product']} --sprint {p['sprint']} --select`",
          "> 本报告只读、未生成任何产物；只有上面的 `--select` 才会驱动弱模型批量生成。",
          "> 跑完每单成功后，其功能特性会写入账本 `coverage-ledger.json`，将来轮到主单所在 sprint 自动跳过。"]
    return "\n".join(L) + "\n"


def write_reports(p: dict) -> dict:
    sd = _state_dir(p["product"])
    md = sd / f"_selection-{p['sprint']}.md"
    js = sd / f"_selection-{p['sprint']}.json"
    md.write_text(_report_md(p), encoding="utf-8")
    js.write_text(json.dumps({**p, "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    return {"md": str(md), "json": str(js)}


# ----------------------------- CLI ----------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Sprint 工单选取器（只读：JQL + 拆单/主单去重 + 覆盖判定）")
    ap.add_argument("--product", default=DEFAULT_PRODUCT)
    ap.add_argument("--sprint", required=True, help="Sprint 日期，如 2026-06-02")
    ap.add_argument("--board", default=None, help="看板 id 覆盖（默认读 config / 本地 rapidViewId）")
    ap.add_argument("--keys-only", action="store_true", help="只打印逗号分隔运行清单（供管道）")
    a = ap.parse_args()

    p = plan(a.product, a.sprint, board=a.board)
    if a.keys_only:
        print(",".join(p["run_list"]))
        return 0
    out = write_reports(p)
    print(_report_md(p))
    print(f"报告 → {Path(out['md']).relative_to(REPO_ROOT).as_posix()}")
    print(f"运行清单（{len(p['run_list'])} 单）：{','.join(p['run_list']) or '（空）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
