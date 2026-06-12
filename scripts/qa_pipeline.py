#!/usr/bin/env python
"""
scripts/qa_pipeline.py

单工单完整弱链编排（多模型协作的“弱扩展”层）。
对一个 Jira 工单跑：
  fetch(直连REST,主+关联) → requirement.md → _jira-search.md → business-context.md
  → analysis.md → questions.md(always,无则“无”) → test-points.md
  → test-design.json(skeleton+detail) → 校验器闸门 → QA 包(_qa-packet.md)

每步可【续跑】：产物已存在则默认跳过（--force 全部重做，--redo a,b 指定步骤重做）。
所有弱模型步骤用 cheap_model（百炼 qwen）；强模型（主 Claude）只读 _qa-packet.md 抽检。
强前置若已由人/强模型产出（requirement/analysis/test-points 已在），本脚本自动跳过、只补 design。

用法：
  python scripts/qa_pipeline.py EAR-240883 --product wms
  python scripts/qa_pipeline.py EAR-240883 --product wms --only design
  python scripts/qa_pipeline.py EAR-240883 --product wms --redo context,analyze
作为库（被 run_sprint 调用）：
  from qa_pipeline import run_pipeline
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
# 工单根目录：可被 QA_TICKETS_ROOT 覆盖（webapp 按用户注入独立目录）；默认仓库 tickets/。
# 知识库 _kb / CLAUDE.md 始终走 REPO_ROOT（跨用户共享）。
TICKETS_ROOT = Path(os.environ.get("QA_TICKETS_ROOT") or (REPO_ROOT / "tickets"))
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

from cheap_model import generate  # noqa: E402
import jira_fetch  # noqa: E402
import batch_generate as bg  # noqa: E402  (复用 _read / run_validator / count_stats / build_ticket)
from core.productcfg import DEFAULT_PRODUCT, get_product  # noqa: E402
try:
    import kb_store  # noqa: E402
except Exception:  # noqa: BLE001
    kb_store = None

KB = "_kb"
JIRA_SEARCH_MD = "_jira-search.md"
JIRA_SEARCH_JSON = "_jira-search.json"
JIRA_SEARCH_FIELDS = (
    "summary,status,issuetype,priority,reporter,assignee,updated,labels,components,"
    "resolution,description,comment,issuelinks,customfield_10020,customfield_10125"
)
MAX_SEARCH_JQL = 8
MAX_SEARCH_RESULTS_PER_JQL = 10
MAX_REFERENCE_ISSUES = 8
MAX_REFERENCE_PER_JQL = 2

STOP_TERMS = {
    "修改方案", "解决方案", "客户场景", "期望目标", "业务规则", "测试用例", "待确认",
    "新增", "增加", "支持", "优化", "调整", "删除", "修改", "查询", "筛选", "导出",
    "页面", "界面", "按钮", "字段", "功能", "需求", "规则", "参数", "工单", "描述",
}

# requirement.md 模板/提示套话：含这些子串的候选词直接丢弃，避免污染 JQL。
STOP_SUBSTRINGS = (
    "唯一权威来源", "本段编写", "本工单业务规则", "以下为", "测试用例必须",
    "权威来源", "仅能围绕", "请围绕", "本段", "如下",
    "拿不准", "降级", "或标", "仅作背景", "仅供", "待人工",
)

# 句末/分隔标点：含这些的候选词会被二次切分或丢弃（JQL text ~ 不适合长句）。
SPLIT_PUNCT = "、，,；;/／？?！!。：:（）()【】「」《》\""

SYNONYM_GROUPS = [
    ("分配", "分摊", "派发", "指派"),
    ("覆盖", "替换", "重写"),
    ("忽略", "跳过", "排除"),
    ("查询", "筛选", "搜索"),
    ("导出", "下载"),
    ("拣货", "分拣"),
]


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _kb_text(rel: str) -> str:
    if kb_store is not None and rel.startswith("_kb/"):
        content = kb_store.read_text(rel, REPO_ROOT / rel)
        if content:
            return content
    return _read(REPO_ROOT / rel)


def _kb_blocks(rel_paths: list[str]) -> str:
    out = []
    for rel in rel_paths:
        content = _kb_text(rel)
        if content:
            out.append(f"\n\n===== {rel} =====\n{content}")
    return "".join(out)


def _section(text: str, heading_re: str) -> str:
    m = re.search(heading_re, text, flags=re.M)
    if not m:
        return ""
    nxt = re.search(r"^##\s+", text[m.end():], flags=re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[m.end():end].strip()


def _clean_term(s: str) -> str:
    s = re.sub(r"https?://\S+", " ", s or "")
    s = re.sub(r"[`*_#{}\[\]<>|\\]", " ", s)
    s = re.sub(r"\s+", "", s)
    return s.strip("：:，,。；;、（）()【】[]“”\"' ")


def _is_noise(term: str) -> bool:
    if not (2 <= len(term) <= 16):
        return True
    if term in STOP_TERMS or term.isdigit() or re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", term):
        return True
    if re.fullmatch(r"L\d{1,2}", term):           # L1/L2/L3 分级标记，非业务词
        return True
    if any(sub in term for sub in STOP_SUBSTRINGS):
        return True
    return False


def _split_subterms(s: str) -> list[str]:
    """把引号内容/长串按标点二次切分成可用于 text ~ 的短词。"""
    parts = re.split(f"[{re.escape(SPLIT_PUNCT)}\\s]+", s or "")
    return [p for p in (_clean_term(x) for x in parts) if p]


def _candidate_terms(text: str) -> list[str]:
    raw: list[str] = []
    # 引号 / 方括号内的内容优先，但要再按标点切分（避免整句带顿号进 JQL）
    for grp in re.findall(r"[“\"]([^“”\"]{2,40})[”\"]", text):
        raw += _split_subterms(grp)
    for grp in re.findall(r"【([^】]{2,40})】", text):
        raw += _split_subterms(grp)
    # 裸中文/英数片段（标点天然断词）
    raw += re.findall(r"[一-鿿A-Za-z0-9_]{2,16}", text)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        term = _clean_term(item)
        if _is_noise(term):
            continue
        if term not in seen:
            out.append(term)
            seen.add(term)
    return out


def _issue_summary(raw: dict) -> str:
    return ((raw.get("fields") or {}).get("summary") or raw.get("key") or "").strip()


def _project_keys(product: str) -> list[str]:
    try:
        import _load_env
        cfg = _load_env.load_raw_config()
    except Exception:
        cfg = {}
    pc = get_product(product)
    keys = pc.jira_project_keys or (cfg.get("jira") or {}).get("default_project_keys") or []
    return [str(k) for k in keys]


def _jql_value(s: str) -> str:
    s = re.sub(r"[\r\n\t]", " ", s or "")
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s.strip()}"'


def _components(raw: dict) -> list[str]:
    return [c.get("name", "") for c in ((raw.get("fields") or {}).get("components") or []) if c.get("name")]


def _expand_terms(terms: list[str]) -> list[str]:
    out = list(terms)
    for term in terms:
        for group in SYNONYM_GROUPS:
            if any(word in term for word in group):
                out.extend(word for word in group if word not in term)
    seen: set[str] = set()
    uniq: list[str] = []
    for term in out:
        term = _clean_term(term)
        if term and term not in seen:
            uniq.append(term)
            seen.add(term)
    return uniq


def _clean_feature(summary: str) -> str:
    """从工单标题提炼一个干净的功能名用于 summary ~ 维度：
    去平台前缀【】/POS-/web- 等、去尾部括号补充、按标点取主词。"""
    s = re.sub(r"^[【\[]([^】\]]+)[】\]]", r"\1 ", summary or "")
    s = re.sub(r"^(?:POS|web|app|新web|接口|公告|PC|H5|TMS)\s*[-—_]\s*", "", s, flags=re.I)
    parts = _split_subterms(s)
    parts = [p for p in parts if not _is_noise(p)]
    return parts[0] if parts else ""


def build_jira_search_plan(ticket_dir: Path, product: str) -> dict:
    raw = {}
    try:
        raw = json.loads(_read(ticket_dir / "_jira-raw.json"))
    except Exception:
        pass
    req = _read(ticket_dir / "requirement.md")
    l1 = _section(req, r"^##\s+3\.\s+修改方案.*$") or req
    summary = _issue_summary(raw)
    comps = _components(raw)
    comp_set = {_clean_term(c) for c in comps}
    l1_terms = _candidate_terms(l1)
    summary_terms = [t for t in _candidate_terms(summary) if t not in comp_set]
    # 核心文本词：来自 L1 + 标题，剔除组件名（组件只走 component 维度）
    core = [t for t in _expand_terms(l1_terms + summary_terms) if t not in comp_set][:8]
    if not core:
        core = summary_terms[:3]
    feature = _clean_feature(summary) or (core[0] if core else summary)
    adjacent = summary_terms[0] if summary_terms else (core[0] if core else feature)
    if adjacent == feature and len(summary_terms) > 1:
        adjacent = summary_terms[1]
    split_terms = core[:2]
    projects = ",".join(_project_keys(product))

    jqls: list[dict] = []
    if comps and core:
        jqls.append({
            "dimension": "component + 核心关键词",
            "jql": f"project in ({projects}) AND component = {_jql_value(comps[0])} AND text ~ {_jql_value(core[0])} ORDER BY updated DESC",
        })
    if feature:
        jqls.append({
            "dimension": "summary 精确匹配",
            "jql": f"project in ({projects}) AND summary ~ {_jql_value(feature)} ORDER BY updated DESC",
        })
    if len(split_terms) >= 2:
        jqls.append({
            "dimension": "拆词搜索",
            "jql": f"project in ({projects}) AND text ~ {_jql_value(split_terms[0])} AND text ~ {_jql_value(split_terms[1])} ORDER BY updated DESC",
        })
    if core:
        jqls.append({
            "dimension": "已解决收敛",
            "jql": f"project in ({projects}) AND text ~ {_jql_value(core[0])} AND statusCategory = Done ORDER BY updated DESC",
        })
    if adjacent:
        jqls.append({
            "dimension": "相邻模块/概念",
            "jql": f"project in ({projects}) AND summary ~ {_jql_value(adjacent)} AND statusCategory = Done ORDER BY updated DESC",
        })
    for term in core[1:4]:
        jqls.append({
            "dimension": "关键词扩展",
            "jql": f"project in ({projects}) AND text ~ {_jql_value(term)} ORDER BY updated DESC",
        })
    return {
        "summary": summary,
        "components": comps,
        "feature": feature,
        "l1_terms": l1_terms[:12],
        "summary_terms": summary_terms[:8],
        "core_terms": core[:8],
        "jqls": jqls[:MAX_SEARCH_JQL],
    }


def _score_issue(issue: dict, terms: list[str]) -> tuple[int, int]:
    f = issue.get("fields") or {}
    summary = f.get("summary") or ""
    resolution = ((f.get("resolution") or {}).get("name") or "")
    itype = ((f.get("issuetype") or {}).get("name") or "")
    score = sum(3 for t in terms if t and t in summary)
    score += sum(1 for t in terms if t and t in (f.get("description") or ""))
    if resolution in ("已修复", "Fixed", "Done"):
        score += 2
    # 汇总/活动类单（版本预排期、需求复核、看板）是噪声，不是业务规则真源——降权
    if itype in ("Activity", "Epic") or any(w in summary for w in ("预排期", "需求复核", "版本复核", "复核", "需求清单")):
        score -= 6
    key_num = int(re.sub(r"\D", "", issue.get("key") or "0") or 0)
    return score, key_num


def run_jira_rule_search(ticket_dir: Path, key: str, product: str, env: dict) -> str:
    plan = build_jira_search_plan(ticket_dir, product)
    jql_rows: list[dict] = []
    selected: dict[str, dict] = {}
    terms = plan.get("core_terms") or []

    for item in plan["jqls"]:
        jql = item["jql"]
        row = {"dimension": item["dimension"], "jql": jql, "total": 0, "selected_keys": [], "error": None}
        try:
            issues = jira_fetch.search(jql, JIRA_SEARCH_FIELDS, env, max_results=MAX_SEARCH_RESULTS_PER_JQL, page_size=20)
        except SystemExit as e:
            row["error"] = str(e).splitlines()[0]
            issues = []
        row["total"] = len(issues)
        ranked = sorted(issues, key=lambda it: _score_issue(it, terms), reverse=True)
        picked_here = 0
        for it in ranked:
            if len(selected) >= MAX_REFERENCE_ISSUES or picked_here >= MAX_REFERENCE_PER_JQL:
                break
            issue_key = it.get("key")
            if not issue_key or issue_key == key or issue_key in selected:
                continue
            if _score_issue(it, terms)[0] <= 0:        # 跳过零分/降权噪声单（汇总/活动单）
                continue
            try:
                selected[issue_key] = jira_fetch.get_issue(issue_key, env, fields=JIRA_SEARCH_FIELDS, expand="comment")
            except SystemExit:
                selected[issue_key] = it
            row["selected_keys"].append(issue_key)
            picked_here += 1
        jql_rows.append(row)

    data = {"key": key, "plan": plan, "jqls": jql_rows, "issues": selected}
    (ticket_dir / JIRA_SEARCH_JSON).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# Jira 业务规则搜索证据 — {key}", ""]
    lines.append("## 1. 关键词识别")
    lines.append(f"- 标题：{plan.get('summary') or '无'}")
    lines.append(f"- 组件：{', '.join(plan.get('components') or []) or '无'}")
    lines.append(f"- L1 核心词：{', '.join(plan.get('core_terms') or []) or '无'}")
    lines.append("")
    lines.append("## 2. 已搜索 JQL")
    if jql_rows:
        for i, row in enumerate(jql_rows, 1):
            suffix = f"；错误：{row['error']}" if row.get("error") else ""
            lines.append(f"{i}. [{row['dimension']}] `{row['jql']}` → 命中 {row['total']} 单，选入：{', '.join(row['selected_keys']) or '无'}{suffix}")
    else:
        lines.append("无（未能从 L1 修改方案提取可搜索关键词）")
    lines.append("")
    lines.append("## 3. 搜索命中工单摘录")
    if selected:
        for issue_key, issue in selected.items():
            lines.append(f"\n### {issue_key}")
            lines.append(jira_fetch.summarize(issue))
    else:
        lines.append("无")
    lines.append("")
    lines.append("## 4. 仍待确认规则")
    if selected:
        lines.append("命中工单只可按 L1/L2/L3 分级引用；命中内容不足以确认的业务规则仍需进入 questions.md。")
    else:
        lines.append("已按上述 JQL 尽职检索但未选入参考工单；业务规则仍不清晰时允许进入 questions.md，并必须引用本文件的 JQL 记录。")
    out = "\n".join(lines).rstrip() + "\n"
    (ticket_dir / JIRA_SEARCH_MD).write_text(out, encoding="utf-8")
    return JIRA_SEARCH_MD


# 每个 markdown 步骤：输出文件、所用 prompt、所需 KB/契约、依赖的前序产物
MD_STEPS = {
    "requirement": {
        "out": "requirement.md",
        "prompts": ["prompts/new-ticket.md"],
        "kb": [f"{KB}/_global/markdown-artifact-schema.md"],
        "inputs": ["_jira-summary.md", "_jira-linked.md"],
        "task": "把下面的 Jira 工单原文整理成 requirement.md，严格按 markdown-artifact-schema 的 requirement 模板，"
                "并按 CLAUDE.md §3.3 做 L1/L2/L3 分级（只有产品撰写的“修改方案/解决方案”段是 L1；评论里产品作者的“方案确认/补充/修订”也是 L1；其余客户场景/背景/接口/AICodeReview/截图为 L2/L3）。"
                "若无明确“修改方案”段，§3 写 [待确认: 本工单无明确修改方案段，请人工确认 L1 范围]，不要自选 L1。",
    },
    "context": {
        "out": "business-context.md",
        "prompts": ["prompts/02-context.md"],
        # rules.md 全量入 system（靠 prompt 缓存复用，跨工单只付一次）
        "kb": [
            f"{KB}/projects/{{product}}/rules.md",
            f"{KB}/projects/{{product}}/modules.md",
            f"{KB}/projects/{{product}}/terms.md",
            f"{KB}/_global/codearts-json-schema.md",
            f"{KB}/_global/case-writing-spec.md",
            f"{KB}/_global/markdown-artifact-schema.md",
        ],
        "inputs": ["requirement.md", "_jira-linked.md", "_jira-search.md"],
        "task": "基于 requirement.md（聚焦 §3 修改方案 L1）+ 关联工单 + _jira-search.md，写 business-context.md。"
                "业务规则要溯源到 rules.md 的具体 §章节号（整段摘录、不重写）；Jira 搜索记录必须来自 _jira-search.md，"
                "不得写“未检索”。Confluence 若本地未提供 MCP 检索结果，则 §4 写“无（批量弱链未接入 Confluence MCP）”。"
                "注意范围边界：只取与本工单 L1 直接相关的规则；后续迭代/相邻规则不要混入。",
    },
    "linked-issues": {
        "out": "linked-issues.md",
        "prompts": ["prompts/02-context.md"],
        "kb": [f"{KB}/_global/markdown-artifact-schema.md"],
        "inputs": ["_jira-linked.md", "_jira-search.md"],
        "task": "把 _jira-linked.md 和 _jira-search.md 整理成 linked-issues.md（markdown-artifact-schema §5：## 1 关联工单 / ## 2 参考工单 / ## 3 已搜索 JQL）。"
                "关联工单“修改方案”段为 L1，其余 L2/L3；## 2 参考工单与 ## 3 已搜索 JQL 必须整理自 _jira-search.md；无命中才写“无”。",
    },
    "analyze": {
        "out": "analysis.md",
        "prompts": ["prompts/01-analyze.md"],
        "kb": [f"{KB}/_global/qa-methodology.md", f"{KB}/_global/markdown-artifact-schema.md"],
        "inputs": ["requirement.md", "business-context.md", "linked-issues.md", "_jira-search.md"],
        "task": "写 analysis.md：功能清单 + 方案切面清单（1:1 覆盖依据，编号）+ 9 维风险 + 待确认问题清单 + 测试范围(覆盖/不覆盖) + 测试点数量预估。"
                "切面只来自 L1；L2/L3 只能进风险/边界/待确认。"
                "【无 L1 铁律】若 requirement.md §3 修改方案 本身是 [待确认]/无可用 L1 规则：禁止从 L3 客户场景或历史 rules.md 自行补出切面当确定规则；方案切面清单写“待方案确认（本工单无 L1）”，待确认清单第 1 条必须是「本工单无明确修改方案，请确认要测试的 L1 范围/规则（含涉及的商品/客户可见性等具体规则）」，其余疑点都依附它、不要凭 L3/历史擅自定论。"
                "【消解优先】业务规则/算法/参数行为/字段联动疑问，列入待确认前必须先在 linked-issues.md（关联工单评论）+ business-context.md（rules.md 摘录）+ _jira-search.md（命中工单）+ 同机制历史规则里找答案；只要任一来源能由 L1/KB 明确规则/已确认评论直接支撑就不要列待确认，直接作为已知规则写入切面并标来源（同机制历史规则仅同模块/同机制且不与本次方案冲突时可作辅证，不能单独定论）；只有全查无据、或依赖需登录原型图、或需产品拍板时才列待确认。不要把能据证据确认的点抛给人工。"
                "待确认问题的「来源」必须是真实可溯源来源、每条带原句加引号：引用评论须点名「工单号+评论人+日期+原句」（如 EAR-252916 陈嘉豪 2026-06-03 评论：“原句”），引用方案/规则须带原句；禁止写“背景信息/关联讨论/相关讨论/有提到”等无主语无出处的笼统标签；L3 背景（客户与背景/背景与价值/客户场景/期望目标/功能详情）只是背景、不是规则，不得当来源。"
                "方案未提的维度（权限/导出/排序/修改记录/必填长度字符集）默认不覆盖、写进“不覆盖”。",
    },
    "questions": {
        "out": "questions.md",
        "prompts": ["prompts/01-analyze.md"],
        "kb": [f"{KB}/_global/markdown-artifact-schema.md"],
        "inputs": ["analysis.md"],
        "task": "根据 analysis.md 的“待确认问题清单”，生成 questions.md。稳定格式硬规则：第 1 行必须是 `# 待确认问题清单 — <工单号>`；无待确认时标题后正文只能写一行 `无`；有问题时只能使用连续 `## QN: <问题陈述>` 题块，Q 从 1 连续递增，不允许其他 `##` 分节。"
                "每个题块必须且只能按顺序包含 `**问题**：`、`**来源**：`、`**可能场景**：`、`**影响范围**：`、`**✅ 答案**：` 五个字段，答案处留 HTML 注释占位。"
                "`**来源**` 逐条列真实可溯源来源、每条带原句加引号：引用评论写「工单号+评论人+日期+“原句”」（如 EAR-252916 陈嘉豪 2026-06-03 评论：“接口功能为查询批发客户信息，支持传入条件‘客户代码’”），引用方案写「本工单修改方案：“原句”」；严禁 JQL/已检索/§/文件名，严禁“背景信息/关联讨论/相关讨论/有提到”等无出处笼统标签，严禁把 L3 背景（客户与背景/客户场景/期望目标/功能详情）当来源，无据则写“无可确认来源，待产品/原型确认”。"
                "只把 analysis.md 已判定“穷尽证据仍无解（需原型图/需产品决策）”的点列入；能从 L1/关联工单/rules/_jira-search 确认的不要当问题问人工。",
    },
    "points": {
        "out": "test-points.md",
        "prompts": ["prompts/03-test-points.md"],
        "kb": [
            f"{KB}/_global/case-writing-spec.md",
            f"{KB}/_global/qa-methodology.md",
            f"{KB}/_global/markdown-artifact-schema.md",
            f"{KB}/projects/{{product}}/modules.md",
            f"{KB}/projects/{{product}}/case-samples/style-notes.md",
        ],
        "inputs": ["requirement.md", "business-context.md", "linked-issues.md", "analysis.md", "questions.md"],
        "task": "写 test-points.md：Pre-flight 5 问 + 测试点列表(标题+优先级+覆盖切面，不展开步骤/预期) + 容器路径(来自 modules.md §2，同名节点以 §2.1 真实为准) + 不覆盖范围 + 仍未决问题。"
                "标题 {平台}_{模块路径}_{测试要点}，平台大小写严格；切面 1:1 映射 analysis §2。",
    },
}

PIPELINE_ORDER = ["fetch", "requirement", "jira-search", "context", "linked-issues", "analyze", "questions", "points", "draft", "design", "validate", "packet"]

SYS_INTRO = (
    "你是资深软件测试工程师（QA）。下面给出项目契约（CLAUDE.md）、本步骤的 prompt 规格、相关 KB/契约文件。"
    "严格遵守它们。你当前在做本工单工作流中的一步，不要调用任何工具/MCP——所需输入都在用户消息里。\n"
    "【输出铁律】只输出该步骤目标产物的正文本身；不要任何解释、不要 Markdown 代码块围栏、不要完成/自查报告、不要前后缀。\n"
    "【UI 文案】按钮/弹窗/标题等用功能性描述直接写，不纠结精确文案、不滥标 [待确认]；[待确认] 只留给业务行为/规则本身不确定处。"
)


def derive_sprint(raw: dict) -> str | None:
    sp = (raw.get("fields") or {}).get("customfield_10125")
    if not sp:
        return None
    text = " ".join(sp) if isinstance(sp, list) else str(sp)
    m = re.search(r"name=(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None


def do_fetch(ticket_dir: Path, key: str, env: dict, main: dict | None = None) -> None:
    if main is None:                       # 允许复用已拉取的主工单，避免重复 REST 请求
        main = jira_fetch.get_issue(key, env)
    (ticket_dir / "_jira-raw.json").write_text(json.dumps(main, ensure_ascii=False, indent=2), encoding="utf-8")
    (ticket_dir / "_jira-summary.md").write_text(jira_fetch.summarize(main), encoding="utf-8")
    # 关联工单（跳过 Won't Do/Duplicate 等可只记标题，这里全摘要）
    linked_blocks = []
    for l in (main.get("fields") or {}).get("issuelinks") or []:
        oi = l.get("outwardIssue") or l.get("inwardIssue")
        if not oi:
            continue
        k = oi.get("key")
        try:
            li = jira_fetch.get_issue(k, env)
            linked_blocks.append(f"\n\n########## 关联工单 {k} ##########\n{jira_fetch.summarize(li)}")
        except SystemExit as e:
            linked_blocks.append(f"\n\n########## 关联工单 {k}（拉取失败）##########\n{e}")
    (ticket_dir / "_jira-linked.md").write_text("".join(linked_blocks) or "无关联工单", encoding="utf-8")


def _sanitize_md(s: str) -> str:
    """markdown 产物禁 HTML 加粗/实体/损坏 span 与 Jira {color} 标记（否则 check-ticket-artifacts FAIL）。
    顺序关键：先解码实体（&amp; 最先，破解 &amp;lt; 双重编码）再剥标签，循环到稳定——
    覆盖“实体编码的 <b>”“双重编码 &amp;lt;”“未闭合 《span”等弱模型高发变体。"""
    def once(x: str) -> str:
        x = x.replace("&amp;", "&")  # 最先：把 &amp;lt; 还原成 &lt; 再被下一行解码
        for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&nbsp;", " ")):
            x = x.replace(a, b)
        x = re.sub(r"</?(?:b|strong)\b[^>]*>", "", x, flags=re.I)
        # 闭合 》 可选并限制在本行内，捕获未闭合/损坏的 《span（校验器只认 “《…span”）
        x = re.sub(r"《\s*/?\s*span(?:span)?\b[^》\n]*》?", "", x)
        return x
    for _ in range(5):  # 收敛到稳定（封顶 5 次，应对多重编码）
        nxt = once(s)
        if nxt == s:
            break
        s = nxt
    s = re.sub(r"\{color[^}]*\}", "", s)
    return s


def run_md_step(name: str, ticket_dir: Path, product: str, max_tokens: int = 12000) -> str:
    spec = MD_STEPS[name]
    sys_prompt = (
        SYS_INTRO
        + _kb_blocks(["CLAUDE.md"])
        + _kb_blocks(spec["prompts"])
        + _kb_blocks([p.format(product=product) for p in spec["kb"]])
    )
    user_parts = [f"【本步任务】{spec['task']}\n"]
    for rel in spec["inputs"]:
        content = _read(ticket_dir / rel)
        if content:
            user_parts.append(f"\n\n===== {rel} =====\n{content}")
    user_parts.append(f"\n\n现在产出 {spec['out']} 的完整正文（只输出正文）：")
    text, _ = generate(system=sys_prompt, user="".join(user_parts), max_tokens=max_tokens)
    # 去掉可能的代码围栏
    body = re.sub(r"^```[a-z]*\s*|\s*```$", "", text.strip(), flags=re.S | re.I)
    body = _sanitize_md(body)
    (ticket_dir / spec["out"]).write_text(body.rstrip() + "\n", encoding="utf-8")
    return spec["out"]


def run_pipeline(key: str, product: str, sprint: str | None = None,
                 only: list[str] | None = None, redo: list[str] | None = None,
                 rounds: int = 3, max_tokens_json: int = 32000) -> dict:
    import _load_env
    env = _load_env.parse_config()
    redo = set(redo or [])
    # 先 fetch 到临时，拿 sprint，再定目录
    ticket_dir = None
    if sprint:
        ticket_dir = TICKETS_ROOT / product / sprint / key
    # 若已存在目录（任一 sprint 下）优先复用
    if ticket_dir is None or not ticket_dir.exists():
        existing = list((TICKETS_ROOT / product).glob(f"*/{key}"))
        if existing:
            ticket_dir = existing[0]
    steps = only or PIPELINE_ORDER
    log = []

    def want(step: str) -> bool:
        return step in steps

    questions_validated = False

    def ensure_questions_format() -> None:
        nonlocal questions_validated
        if questions_validated:
            return
        q = ticket_dir / "questions.md"
        if not q.exists():
            raise RuntimeError(f"[{key}] questions.md 缺失，无法继续生成 points/design")
        nrc, nout = bg.run_validator("normalize-questions.py", str(q))
        if nrc != 0:
            raise RuntimeError(f"[{key}] questions.md 自动格式归一失败，已阻断后续生成：\n{nout}")
        log.append("questions-normalize:changed" if "CHANGED:" in nout else "questions-normalize:ok")
        rc, out = bg.run_validator("validate-questions.py", str(q))
        if rc != 0:
            raise RuntimeError(
                f"[{key}] questions.md 格式自动修复后仍不稳定，已阻断后续生成：\n"
                f"--- normalize-questions.py ---\n{nout}\n"
                f"--- validate-questions.py ---\n{out}"
            )
        questions_validated = True
        log.append("questions-format:ok")

    # fetch（决定 sprint/目录）
    if want("fetch"):
        # 需要先知道目录；若未知，先拉一次 raw 求 sprint
        if ticket_dir is None or not ticket_dir.exists():
            tmp = jira_fetch.get_issue(key, env)
            sp = sprint or derive_sprint(tmp) or "_unsorted"
            ticket_dir = TICKETS_ROOT / product / sp / key
            ticket_dir.mkdir(parents=True, exist_ok=True)
            (ticket_dir / "attachments").mkdir(exist_ok=True)
            do_fetch(ticket_dir, key, env, main=tmp)  # 复用已拉取的主工单（求 sprint 时已拉），只补关联
            log.append("fetch:done")
        elif redo & {"fetch"} or not (ticket_dir / "_jira-raw.json").exists():
            ticket_dir.mkdir(parents=True, exist_ok=True)
            (ticket_dir / "attachments").mkdir(exist_ok=True)
            do_fetch(ticket_dir, key, env)
            log.append("fetch:done")
        else:
            log.append("fetch:skip")
    if ticket_dir is None:
        raise SystemExit(f"[qa_pipeline] 无法定位 {key} 的目录（请指定 --sprint 或先 fetch）")

    # requirement 必须先于 Jira 规则检索生成：搜索关键词只从 L1 修改方案出发。
    if want("requirement"):
        out = ticket_dir / MD_STEPS["requirement"]["out"]
        if out.exists() and out.stat().st_size > 3 and "requirement" not in redo:
            log.append("requirement:skip")
        else:
            run_md_step("requirement", ticket_dir, product)
            log.append("requirement:gen")
            print(f"[{key}] requirement → {MD_STEPS['requirement']['out']}")

    downstream_needs_search = any(want(s) for s in ["context", "linked-issues", "analyze", "questions"])
    search_md = ticket_dir / JIRA_SEARCH_MD
    if want("jira-search") or downstream_needs_search:
        if search_md.exists() and search_md.stat().st_size > 3 and "jira-search" not in redo:
            log.append("jira-search:skip")
        else:
            run_jira_rule_search(ticket_dir, key, product, env)
            log.append("jira-search:done")
            print(f"[{key}] jira-search → {JIRA_SEARCH_MD}")

    # markdown 步骤
    for name in ["context", "linked-issues", "analyze", "questions", "points"]:
        if not want(name):
            continue
        if name == "points":
            ensure_questions_format()
        out = ticket_dir / MD_STEPS[name]["out"]
        if out.exists() and out.stat().st_size > 3 and name not in redo:
            log.append(f"{name}:skip")
            if name == "questions":
                ensure_questions_format()
            continue
        run_md_step(name, ticket_dir, product)
        log.append(f"{name}:gen")
        print(f"[{key}] {name} → {MD_STEPS[name]['out']}")
        if name == "questions":
            ensure_questions_format()

    # design（JSON skeleton+detail）+ validate（build_ticket 内含校验闭环）
    res = {"key": key, "dir": str(ticket_dir), "log": log}
    # 草稿用例（_draft-design.json）：在「生成→人工确认」之前先出一版草稿，供强模型复核，把
    # 「看草稿才暴露、需人工拍板」的点汇总进 questions.md，使人工只答一次。草稿【不叫】
    # test-design.json，故对看板/徽标/digest/覆盖账本完全不可见——「已生成」唯一真源仍是
    # test-design.json，工单仍停在「待确认」状态。继续生成(--resume-after-questions)才出正式用例。
    if want("draft"):
        ensure_questions_format()
        dd = ticket_dir / "_draft-design.json"
        if dd.exists() and dd.stat().st_size > 5 and "draft" not in redo:
            log.append("draft:skip")
        else:
            r = bg.build_ticket(ticket_dir, out_name="_draft-design.json", rounds=rounds,
                                max_tokens=max_tokens_json, force=True)
            res["draft"] = r
            log.append(f"draft:{'ok' if r.get('ok') else 'fail'}")
    if want("design"):
        ensure_questions_format()
        td = ticket_dir / "test-design.json"
        if td.exists() and td.stat().st_size > 5 and "design" not in redo:
            log.append("design:skip")
        else:
            r = bg.build_ticket(ticket_dir, out_name="test-design.json", rounds=rounds,
                                max_tokens=max_tokens_json, force=True)
            res["design"] = r
            log.append(f"design:{'ok' if r.get('ok') else 'fail'}")

    # QA 包
    ensure_readme(ticket_dir, key)
    if want("packet"):
        res["packet"] = emit_packet(ticket_dir, key)
        log.append("packet:done")
    return res


def ensure_readme(ticket_dir: Path, key: str) -> None:
    p = ticket_dir / "README.md"
    if p.exists() and p.stat().st_size > 3:
        return
    p.write_text(
        f"# {key}\n\n## SOP 进度\n\n"
        "- [x] /qa:context\n- [x] /qa:analyze\n- [x] /qa:points\n- [x] /qa:skeleton\n- [x] /qa:detail\n"
        "- [ ] 粘贴 CodeArts + 评审\n- [ ] /qa:kb-extract\n\n"
        "## 关键决策记录\n\n（由多模型流水线 qa_pipeline 自动生成；详见各产物与 _qa-packet.md）\n\n"
        "## 附加材料\n\n- `_jira-raw.json` / `_jira-summary.md` / `_jira-linked.md`：Jira 原始拉取\n"
        "- `_qa-packet.md`：强模型抽检包\n",
        encoding="utf-8",
    )


def emit_packet(ticket_dir: Path, key: str) -> str:
    """生成紧凑 QA 包供强模型（主 Claude）抽检。"""
    td_path = ticket_dir / "test-design.json"
    product = ticket_dir.parent.parent.name
    lines = [f"# QA 包 · {key}", ""]
    # 校验器结果
    rc1, out1 = bg.run_validator("validate-test-design.py", str(td_path))
    rc2, out2 = bg.run_validator("check-ticket-artifacts.py", str(ticket_dir))
    rc3, out3 = bg.run_validator("validate-containers.py", str(td_path), "--product", product, "--quiet")
    lines.append(f"- validate-test-design.py: exit={rc1}（0=结构合规）")
    lines.append(f"- check-ticket-artifacts.py: exit={rc2}")
    lines.append(f"- validate-containers.py: exit={rc3}（0=容器路径合规或仅 WARN）")
    fails = [l for l in (out1 + out2 + out3).splitlines() if l.startswith("FAIL:")]
    cwarns = [l for l in out3.splitlines() if l.startswith("WARN:")]
    if fails:
        lines.append("- FAIL:")
        lines += [f"    {l}" for l in fails[:20]]
    if cwarns:
        lines.append("- 容器 WARN（§2 未收录该节点，需人工确认：补进 §2 或修正容器路径）：")
        lines += [f"    {l}" for l in cwarns[:20]]
    # 统计
    try:
        data = json.loads(_read(td_path))
        st = bg.count_stats(data)
        lines.append(f"- 统计：测试点 {st['testpoints']} / step {st['steps']} / expect {st['expects']} / 待确认 {st['todo']}")
        # 测试点标题清单
        titles = []

        def walk(n):
            if isinstance(n, list):
                [walk(x) for x in n]
            elif isinstance(n, dict):
                if "testPoint" in n or "mark" in n:
                    titles.append(str(n.get("text", "")))
                [walk(c) for c in n.get("children", []) or []]
        walk(data)
        lines.append("")
        lines.append("## 测试点标题")
        lines += [f"{i+1}. {t}" for i, t in enumerate(titles)]
    except Exception as e:
        lines.append(f"- （解析 test-design.json 失败：{e}）")
    # analysis 切面清单 + 待确认，供强模型核对覆盖
    an = _read(ticket_dir / "analysis.md")
    if an:
        lines.append("")
        lines.append("## 强模型抽检要点")
        lines.append("- 核对：测试点是否 1:1 覆盖 analysis.md 的方案切面清单（不多测方案没提的、不漏方案提的）")
        lines.append("- 核对：算术/状态流转/L2L3 是否误入 expect / 容器路径是否 ⊆ modules.md §2.1")
        lines.append("- 关联已解决 bug 是否补回归用例；[待确认] 是否被偷偷编造为确定值")
    packet = ticket_dir / "_qa-packet.md"
    packet.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(packet)


def main() -> int:
    ap = argparse.ArgumentParser(description="单工单完整弱链 + 校验 + QA 包")
    ap.add_argument("key", help="工单号，如 EAR-240883")
    ap.add_argument("--product", default=DEFAULT_PRODUCT)
    ap.add_argument("--sprint", default=None, help="Sprint 日期目录（不填则从 Jira Sprint 字段推导）")
    ap.add_argument("--only", default=None, help="只跑这些步骤（逗号分隔）：fetch,requirement,jira-search,context,linked-issues,analyze,questions,points,design,validate,packet")
    ap.add_argument("--redo", default=None, help="强制重做这些步骤（逗号分隔）")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--max-tokens-json", type=int, default=32000)
    a = ap.parse_args()
    only = [s.strip() for s in a.only.split(",")] if a.only else None
    redo = [s.strip() for s in a.redo.split(",")] if a.redo else None
    res = run_pipeline(a.key, a.product, a.sprint, only=only, redo=redo,
                       rounds=a.rounds, max_tokens_json=a.max_tokens_json)
    print(f"\n[{a.key}] 完成：{' '.join(res['log'])}")
    if res.get("packet"):
        print(f"[{a.key}] QA 包：{res['packet']}")
    d = res.get("design")
    return 0 if (d is None or d.get("ok")) else 1


if __name__ == "__main__":
    sys.exit(main())
