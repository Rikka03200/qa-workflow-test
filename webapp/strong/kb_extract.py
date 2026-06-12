"""知识回填（/qa:kb-extract 的 webapp 端口，写【共享】_kb/projects/<product>/rules.md —— 最高风险）。

两段式、人工闸门：
  1) propose（强模型，只读）：从工单已确认产物（修改方案 L1 + 已答待确认 + 复核）提炼跨工单可复用的
     候选业务规则，带【可溯源原句】，写 kb-proposal.json / kb-proposal.md，不入库。
  2) apply（确定性 Python，人工逐条勾选后调用）：把选中的规则追加进共享 rules.md。

写库铁律：①只追加、不重写；②进程锁串行（防多用户并发损坏共享库）；③写前整文件 .bak；
④规则以 `### 小节` 累积在专属章节下（不新增散落 `## 章节`，免动 §0 目录索引）；
⑤写后跑 scripts/kb-check-toc.py 校验，**不过则整体回滚**（rules.md 一字不动）。
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .. import config
from . import runner
from .spot_check import _arts_block, _ear
from core.productcfg import DEFAULT_PRODUCT

_PROPOSE_ARTS = ["requirement.md", "questions.md", "_spot-check.md", "analysis.md",
                 "linked-issues.md", "business-context.md"]

# 候选规则提案结构（拼进 prompt 作 shape 提示）
KB_PROPOSAL_SCHEMA = {
    "rules": [
        {"title": "规则小节标题（简短业务名，如 查询单位表头联动）",
         "content": "拟写入知识库的规则正文（markdown，保留方案原文、不美化、不得含 [待确认]）",
         "source": "可溯源出处原句：本工单修改方案，或 EAR-xxx 某人 某日 评论 的原句",
         "reusable_reason": "为何跨工单可复用（一句话）",
         "conflict": "与现有规则冲突的说明；无则空字符串"}
    ],
    "terms": [{"term": "术语", "definition": "释义（保留原文）", "source": "出处"}],
    "notes": "整体说明；确无可入库时写明原因",
}
_HINT = json.dumps(KB_PROPOSAL_SCHEMA, ensure_ascii=False)

_KB_LOCK = threading.Lock()          # 共享 rules.md 写入串行化（单实例进程内）
_BACKFILL_TITLE = "知识回填（系统沉淀）"


# ----------------------------- propose（强模型，只读） -----------------------------

def _propose_prompt(ear: str, arts: str) -> str:
    return (
        f"你是资深 QA + 知识库维护者。从工单 {ear} 的【已确认】产物里，提炼出**值得沉淀进知识库**的"
        f"跨工单可复用业务规则。\n\n"
        f"硬规则：\n"
        f"- **只取 L1**：产品撰写的『修改方案/解决方案』段，或评论中产品作者明确的方案确认/补充。"
        f"严禁从客户场景/背景/接口名/截图(L2/L3)、或从 test-design.json 反向提炼规则。\n"
        f"- 每条规则必须带【可溯源原句】：写清是『本工单修改方案：“原句”』或『EAR-xxx 某人 某日 评论：“原句”』。\n"
        f"- 只收**跨工单可复用、覆盖产品行为**的规则；一次性工单细节不收。\n"
        f"- 仍标 [待确认] 的、未澄清的，不收。content 里不得出现 [待确认]、工单号、§、文件名。\n"
        f"- 与现有规则可能冲突的，在 conflict 里说明，不要擅自下结论。\n"
        f"- 宁缺勿滥：确实没有可复用新规则就返回空 rules 并在 notes 说明。\n\n"
        f"以下是该工单工件（已内嵌，无需读取文件）：\n{arts}\n\n"
        f"输出候选规则(rules)、候选术语(terms)、说明(notes)。"
    )


def _write_proposal_md(directory: Path, proposal: dict) -> None:
    ear = proposal.get("ear", directory.name)
    L = [f"# 知识回填提案 — {ear}", "",
         f"> 候选规则 {len(proposal.get('rules') or [])} 条 · 候选术语 {len(proposal.get('terms') or [])} 条"
         f" · 生成 {proposal.get('generated', '')}", ""]
    if proposal.get("notes"):
        L += [proposal["notes"], ""]
    for i, r in enumerate(proposal.get("rules") or [], 1):
        L.append(f"## 候选规则 #{i}：{r.get('title', '')}")
        L.append(r.get("content", ""))
        L.append(f"- 来源：{r.get('source', '')}")
        if r.get("reusable_reason"):
            L.append(f"- 可复用：{r['reusable_reason']}")
        if r.get("conflict"):
            L.append(f"- ⚠ 冲突：{r['conflict']}")
        L.append("")
    (directory / "kb-proposal.md").write_text("\n".join(L).rstrip() + "\n", encoding="utf-8")


async def propose_one(dir_str: str, on_log=None) -> dict:
    ear = _ear(dir_str)
    arts = _arts_block(dir_str, _PROPOSE_ARTS)
    r = await runner.query_json(_propose_prompt(ear, arts), shape_hint=_HINT, allowed_tools=[])
    proposal = {
        "ear": ear,
        "rules": (r or {}).get("rules") or [],
        "terms": (r or {}).get("terms") or [],
        "notes": (r or {}).get("notes") or "",
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    p = Path(dir_str)
    p.joinpath("kb-proposal.json").write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_proposal_md(p, proposal)
    if on_log:
        on_log(f"{ear}: 提炼候选规则 {len(proposal['rules'])} 条 / 术语 {len(proposal['terms'])} 条")
    return proposal


async def run(ticket_dirs: list[str], product: str = DEFAULT_PRODUCT,
              on_log: Optional[Callable[[str], None]] = None) -> list[dict]:
    if not ticket_dirs:
        return []
    results = await asyncio.gather(*[propose_one(d, on_log) for d in ticket_dirs],
                                   return_exceptions=True)
    if on_log:
        for r in results:
            if isinstance(r, Exception):
                on_log(f"提炼某单出错（已跳过）：{type(r).__name__}: {str(r)[:160]}")
    return [r for r in results if isinstance(r, dict)]


def read_proposal(dir_str: str) -> Optional[dict]:
    p = Path(dir_str) / "kb-proposal.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ----------------------------- apply（确定性写共享 rules.md） -----------------------------

_CHAP = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$", re.M)


def rules_path(product: str) -> Path:
    return config.REPO_ROOT / "_kb" / "projects" / product / "rules.md"


def _find_backfill_chapter(text: str) -> Optional[int]:
    for m in _CHAP.finditer(text):
        if m.group(2).strip().startswith("知识回填"):
            return int(m.group(1))
    return None


def _next_chapter_no(text: str) -> int:
    nums = [int(m.group(1)) for m in _CHAP.finditer(text)]
    return (max(nums) + 1) if nums else 1


def _chapter_region(text: str, chap: int) -> tuple[int, int]:
    """返回 ## chap. 章节正文区间 [start_after_header, end)（end=下一个 ## N. 或文末）。"""
    m = re.search(rf"^##\s+{chap}\.\s+.*$", text, re.M)
    if not m:
        return (len(text), len(text))
    nxt = re.search(r"^##\s+\d+\.\s+", text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return (m.end(), end)


def _insert_toc_ref(text: str, chap: int, title: str) -> str:
    """在 §0 目录区（## 0. 目录索引 → ## 1. 之间）末尾插一条 §chap 引用，满足 kb-check-toc。"""
    m1 = re.search(r"^##\s+1\.\s+", text, re.M)
    line = f"- §{chap} {title}（控制台知识回填累积）\n"
    if not m1:
        return text                       # 无 §1 锚点：交给 toc-check 兜底（不过则回滚）
    return text[:m1.start()] + line + "\n" + text[m1.start():]


def apply(product: str, ear: str, rules: list, date: str = "") -> dict:
    """把选中的候选规则追加进共享知识库（PostgreSQL 优先 + markdown 导出兜底）。"""
    res = {"applied": 0, "chapter": None, "notes": "", "ok": False}
    rules = [r for r in (rules or []) if (r or {}).get("content")]
    if not rules:
        res["notes"] = "没有选中可入库的规则"
        return res
    with _KB_LOCK:
        rp = rules_path(product)
        real_rp = config.REPO_ROOT / "_kb" / "projects" / product / "rules.md"
        if rp.resolve() == real_rp.resolve():
            try:
                from webapp.services import scripts_loader
                kb_store = scripts_loader.load_normal("kb_store")
            except Exception:  # noqa: BLE001
                kb_store = None
            if kb_store is not None:
                try:
                    out = kb_store.append_backfill_rules(product, ear, rules, date)
                    if out.get("ok"):
                        return out
                    if out.get("notes"):
                        return out
                except Exception:  # noqa: BLE001
                    pass

        if not rp.exists():
            res["notes"] = f"知识库文件不存在：{rp.as_posix()}"
            return res
        today = date or datetime.now().strftime("%Y-%m-%d")
        text = rp.read_text(encoding="utf-8")
        orig = text
        chap = _find_backfill_chapter(text)
        if chap is None:
            chap = _next_chapter_no(text)
            text = _insert_toc_ref(text, chap, _BACKFILL_TITLE)
            text = text.rstrip() + (
                f"\n\n## {chap}. {_BACKFILL_TITLE}\n\n"
                f"> 本章由控制台「知识回填」累积：从工单修改方案/已确认待确认提炼、人工审批后入库；"
                f"每条标注来源与工单，可后续人工归并到对应业务章节。\n")
        # 现有 ### chap.N 小节数 → 续号
        rstart, rend = _chapter_region(text, chap)
        seq = len(re.findall(rf"^###\s+{chap}\.\d+", text[rstart:rend], re.M))
        blocks = []
        for r in rules:
            seq += 1
            title = (r.get("title") or "新增规则").strip()
            content = (r.get("content") or "").strip()
            source = (r.get("source") or "").strip()
            b = [f"\n### {chap}.{seq} {title}", "", content, "",
                 f"- 来源：{source}", f"- 入库：{ear} · {today}"]
            if r.get("conflict"):
                b.append(f"- ⚠ 待人工裁定的冲突：{r['conflict']}")
            blocks.append("\n".join(b) + "\n")
        # 插到该章节正文末尾
        rstart, rend = _chapter_region(text, chap)
        text = text[:rend].rstrip() + "\n" + "".join(blocks) + "\n" + text[rend:]

        # 写 tmp → toc 校验 → .bak + 替换 或 回滚
        tmp = rp.with_name(f"{rp.name}.{uuid.uuid4().hex}.kb.tmp")
        tmp.write_text(text, encoding="utf-8")
        toc = config.REPO_ROOT / "scripts" / "kb-check-toc.py"
        try:
            proc = subprocess.run([sys.executable, str(toc), "--path", str(tmp)],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=60)
            toc_ok = proc.returncode == 0
            toc_out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as e:  # noqa: BLE001
            toc_ok, toc_out = False, f"目录校验执行失败：{e}"
        if not toc_ok:
            tmp.unlink(missing_ok=True)
            res["notes"] = "写入后目录校验未通过，已放弃（知识库未改动）：" + toc_out.strip()[-200:]
            return res
        try:
            rp.with_name(rp.name + ".bak").write_text(orig, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        import os
        os.replace(tmp, rp)
    res.update({"applied": len(rules), "chapter": chap, "ok": True})
    return res
