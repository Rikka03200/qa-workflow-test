"""复核后修复（webapp 专属，写 test-design.json —— 高风险，靠校验+回滚兜底）。

强模型只输出「改哪个已存在节点(id) + 新文本」，确定性 Python 按 id 定位改 text、
保留其余节点与全部 UUID/结构不变，再跑 validate-test-design 校验——过了才 .bak+写、
不过整体回滚。**绝不增删节点、绝不可能写出结构非法的用例树。**
需要结构性新增（漏覆盖切面/要加测试点）的发现放 unfixable 列出，交重新生成/人工，不硬塞文本。

铁律：①只改已存在节点 text；②禁止在新文本引入工单号/§/[来源:]/文件名/截图名（确定性护栏）；
③写前 .bak、写后必过结构校验否则回滚；④只依据已确认的 _spot-check.md。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import runner, schemas
from .spot_check import _arts_block, _ear
from ..services import scripts_loader

# 复核要看的工件（含 _spot-check.md 的确认发现 + 当前用例 + L1 依据）
_ARTS = ["test-design.json", "_spot-check.md", "requirement.md", "questions.md",
         "business-context.md", "analysis.md", "linked-issues.md", "test-points.md"]
_VERIFY_ARTS = ["test-design.json", "_spot-check.md", "_revise.md", "requirement.md",
                "questions.md", "business-context.md", "analysis.md", "linked-issues.md",
                "test-points.md"]
_REVISE_HINT = json.dumps(schemas.REVISE_SCHEMA, ensure_ascii=False)
_VERIFY_HINT = json.dumps(schemas.REVISE_VERIFY_SCHEMA, ensure_ascii=False)

# 新文本里不允许出现的开发引用/外部定位（混进 CodeArts 正文即不合格）——确定性护栏，命中则不应用该处。
# 含 `[待确认`：复核【绝不】往用例里新增待确认图章（那是死胡同、用户无法回答）；需人工的点走 questions。
_FORBIDDEN_IN_TEXT = re.compile(
    r"[A-Z][A-Z0-9]+-\d+|§|\[来源|\[待确认|\b\w+\.md\b|\b\w+\.png\b|test-design|questions\.md", re.I)
_TRACEABLE_SOURCE_RE = re.compile(
    r"需求方案|修改方案|解决方案|关联工单|评论|业务规则|rules\.md|requirement\.md|"
    r"business-context\.md|linked-issues\.md|questions\.md|待确认答案|Jira\s*[A-Z][A-Z0-9]+-\d+|[A-Z][A-Z0-9]+-\d+"
)
_SOURCE_ONLY_RE = re.compile(r"^(?:复核发现|草稿复核|方案未明确|证据完整性审查|AI|模型|建议)[^“”\"']*$")


def _revise_prompt(ear: str, arts: str) -> str:
    return (
        f"你是资深 QA。工单 {ear} 的复核（_spot-check.md）已确认若干问题。"
        f"请把它们分流处理，输出节点级修订 + 需人工的问题 + 需重新生成的项。\n\n"
        f"硬规则：\n"
        f"- edits：只对【已存在节点】(用其 id 字段，32 位 hex 定位)做【有 L1 方案 / 已确认 questions 答案支撑】"
        f"的纯文字修正。不得新增/删除节点，不得改 id/结构/marker；改完每个 step 仍恰好 1 个 expect。\n"
        f"- 对每条 _spot-check.md 已确认建议必须逐条闭环：能改文字的给 edits；需人工拍板的给 questions；"
        f"需结构增删的给 unfixable。不能只修其中一个示例节点就算完成。\n"
        f"- 若一个确认建议或同一 L1 依据影响多个平行测试点/节点（例如门店与客户、web 与 app、开启与关闭分支），"
        f"必须扫描 test-design.json 中所有同类节点：凡是同一未确认表述仍残留且可纯改文字修复的，都必须给 edits。\n"
        f"- 新 text 仍守用例正文铁律：换行只用 <br>，内容自包含，禁工单号(EAR-xxx)/§/[来源:]/文件名(.md)/截图名(.png)。\n"
        f"- 【绝不在 text 里写 [待确认]】：方案没定/无 L1 依据/需产品或原型拍板的点，**不要塞进用例**，"
        f"放进 questions（会折进 questions.md 让用户回答）。把不确定写成确定值也不行——同样放 questions。\n"
        f"- questions：给业务语言的一句话问题 + problem/source/possible_scenarios/impact；possible_scenarios 必须给 2 个以上可选项。"
        f"source 必须是用户能找得到的真实依据：需求方案原句、关联工单 EAR-xxxxxx 的方案/评论原话、业务规则、或已确认待确认答案；"
        f"禁止写『复核发现』『AI 建议』『方案未明确』这类不可追溯来源。"
        f"特别地，若复核指出『本工单无明确修改方案 / L1 范围待确认 / 用例把 L3 客户场景或历史规则写成确定 expect』，"
        f"必须在 questions 里提『本工单无明确修改方案，请确认要测试的 L1 范围/规则』。\n"
        f"- unfixable：需结构性增删（漏覆盖切面、越界需删整条测试点）才能修的，放这里（交重新生成），不要硬塞文本。\n"
        f"- 只动复核确认的问题涉及的节点及同源同类残留节点，其余一律原样不动。\n\n"
        f"以下是该工单工件（已内嵌，无需读取文件）：\n{arts}\n\n"
        f"对每个要改的节点给：node_id、old_text_snippet（原文片段便于核对定位）、new_text（完整新文本）、"
        f"reason（依据哪条复核发现 + 哪条 L1/已确认答案）。确实没有可只改文本修复的就返回空 edits。"
    )


def _verify_prompt(ear: str, arts: str) -> str:
    return (
        f"你是复核修复后的验收员。工单 {ear} 已执行 _spot-check.md 的自动修复，"
        f"现在必须验收：所有【已确认建议】是否已被修复或正确分流，不能只看 _revise.md 自称。\n\n"
        f"验收规则：\n"
        f"- 对 _spot-check.md 中每条确认建议逐条核对当前 test-design.json。\n"
        f"- 若问题已通过 edits 消除，算 resolved。\n"
        f"- 若问题确实需要人工拍板且已进入 questions.md，算已分流。\n"
        f"- 若问题必须新增/删除测试点、步骤或预期，且已在 _revise.md 的『需重新生成 / 人工补』列出，算已分流；"
        f"但不要把它当作可自动修复完成。\n"
        f"- 若同一 L1 依据下的平行节点仍残留相同未确认/越界文本（例如只修了门店，客户侧仍有同样的『弹窗关闭』），"
        f"这就是未消解。若可只改已有节点 text 修复，leftover.fix_type=text，并给 node_id 与 suggested_new_text。\n"
        f"- 若仍未消解但需要人工确认，fix_type=question；若需要结构增删，fix_type=structural；拿不准 fix_type=unknown。\n"
        f"- leftover.source 必须是真实可追溯来源：需求方案原句、关联工单评论、业务规则或已确认待确认答案；"
        f"禁止写『复核发现』『AI 建议』『方案未明确』。\n\n"
        f"以下是工件（已内嵌，无需读取文件）：\n{arts}\n\n"
        f"只输出 JSON。若没有未消解项，resolved=true 且 leftover=[]。"
    )


def _traceable_source(source: str) -> bool:
    src = (source or "").strip()
    if not src or _SOURCE_ONLY_RE.match(src):
        return False
    return bool(_TRACEABLE_SOURCE_RE.search(src))


def _index_by_id(data) -> dict:
    """递归把 test-design 树里所有带 id 的节点收成 {id: node}。"""
    idx: dict = {}

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            nid = n.get("id")
            if isinstance(nid, str) and nid:
                idx[nid] = n
            for c in (n.get("children") or []):
                walk(c)

    walk(data)
    return idx


def apply_edits(td_path: Path, edits: list) -> dict:
    """确定性应用节点 text 修订：按 id 定位改 text → 字节级序列化 → 结构校验 → .bak + 替换或回滚。"""
    res = {"applied": 0, "skipped": 0, "notes": "", "changed": []}
    if not td_path.exists():
        res["notes"] = "test-design.json 不存在"
        return res
    try:
        data = json.loads(td_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        res["notes"] = f"读取用例 JSON 失败：{e}"
        return res
    idx = _index_by_id(data)
    applied_ids = []
    for e in (edits or []):
        nid = (e or {}).get("node_id")
        nt = (e or {}).get("new_text")
        node = idx.get(nid)
        if node is None or not isinstance(nt, str) or not nt.strip():
            res["skipped"] += 1
            continue
        if _FORBIDDEN_IN_TEXT.search(nt):     # 护栏：不引入开发引用/外部定位
            res["skipped"] += 1
            continue
        node["text"] = nt
        applied_ids.append(nid)
        res["changed"].append({"node_id": nid, "reason": (e or {}).get("reason", "")})
    res["applied"] = len(applied_ids)
    if not applied_ids:
        res["notes"] = (res["notes"] + " 无可应用的节点修订（可能修订需结构性改动，见下方提示）。").strip()
        return res

    # 字节级契约：json.dumps(ensure_ascii=False, indent=2) + "\n"
    new_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    orig = td_path.read_text(encoding="utf-8")
    tmp = td_path.with_name(f"{td_path.name}.{uuid.uuid4().hex}.revise.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    bg = scripts_loader.batch_generate()
    rc, out = bg.run_validator("validate-test-design.py", str(tmp))
    if rc != 0:
        tmp.unlink(missing_ok=True)
        res["applied"] = 0
        res["changed"] = []
        res["notes"] = "修复后结构校验未通过，已放弃（用例未改动）：" + str(out)[:200]
        return res
    try:
        td_path.with_name(td_path.name + ".bak").write_text(orig, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    os.replace(tmp, td_path)   # 原子替换
    return res


def _merge_apply_result(res: dict, extra: dict) -> None:
    res["applied"] = int(res.get("applied") or 0) + int(extra.get("applied") or 0)
    res["skipped"] = int(res.get("skipped") or 0) + int(extra.get("skipped") or 0)
    res.setdefault("changed", []).extend(extra.get("changed") or [])
    note = (extra.get("notes") or "").strip()
    if note:
        base = (res.get("notes") or "").strip()
        res["notes"] = f"{base}；修后验收补漏：{note}" if base else f"修后验收补漏：{note}"


def _normalize_leftover(verdict: dict) -> list[dict]:
    raw = (verdict or {}).get("leftover") or []
    leftover = [x for x in raw if isinstance(x, dict) and any(x.get(k) for k in ("finding", "why", "node_id"))]
    if (verdict or {}).get("resolved") is False and not leftover:
        leftover.append({"finding": "修后验收未通过但模型未给出明细", "fix_type": "unknown",
                         "why": (verdict or {}).get("reasoning", "未提供原因")})
    return leftover


def _leftover_text_edits(leftover: list[dict]) -> list[dict]:
    edits = []
    for item in leftover or []:
        if str(item.get("fix_type", "")).strip().lower() != "text":
            continue
        source = (item.get("source") or "").strip()
        if not _traceable_source(source):
            continue
        nid = (item.get("node_id") or "").strip()
        new_text = item.get("suggested_new_text")
        if not nid or not isinstance(new_text, str) or not new_text.strip():
            continue
        edits.append({
            "node_id": nid,
            "old_text_snippet": item.get("current_text", ""),
            "new_text": new_text,
            "reason": f"修后验收补漏：{item.get('finding', '')}；依据：{source}；{item.get('why', '')}",
        })
    return edits


def _write_md(directory: Path, ear: str, res: dict, unfixable: list) -> None:
    leftover = res.get("leftover") or []
    L = [f"# 复核修复记录 — {ear}", "",
         f"> 已应用 {res.get('applied', 0)} 处节点修订"
         + (f"；跳过 {res['skipped']} 处" if res.get("skipped") else "")
         + (f"；{len(unfixable)} 处需重新生成/人工" if unfixable else "")
         + (f"；{len(leftover)} 处修后验收未消解" if leftover else "")
         + ("；修后验收失败" if res.get("postcheck_error") else ""), ""]
    if res.get("notes"):
        L += [f"说明：{res['notes']}", ""]
    if res.get("changed"):
        L.append("## 已修复")
        L += [f"- 依据：{c.get('reason', '')}" for c in res["changed"]]
        L.append("")
    if unfixable:
        L.append("## 需重新生成 / 人工补（仅改文本无法修复）")
        for u in unfixable:
            L.append(f"- {u.get('finding', '')} —— {u.get('why', '')}")
        L.append("")
    if res.get("postcheck_error") or res.get("postcheck") is not None:
        L.append("## 修后验收")
        if res.get("postcheck_error"):
            L.append("⚠️ 修后验收失败，不能视为已全部修复。")
            L.append(f"- 原因：{res['postcheck_error']}")
        elif leftover:
            L.append("⚠️ 仍需处理：修后验收仍发现未消解项。")
            for item in leftover:
                fix_type = item.get("fix_type", "unknown")
                where = f"（节点 {item.get('node_id')}）" if item.get("node_id") else ""
                L.append(f"- [{fix_type}] {item.get('finding', '')}{where} —— {item.get('why', '')}")
        else:
            reasoning = ((res.get("postcheck") or {}).get("reasoning") or "确认建议已修复或正确分流。")
            L.append(f"✅ 修后验收通过：{reasoning}")
        L.append("")
    (directory / "_revise.md").write_text("\n".join(L).rstrip() + "\n", encoding="utf-8")


async def _verify_repair(dir_str: str, ear: str) -> dict:
    arts = _arts_block(dir_str, _VERIFY_ARTS, cap=12000)
    return await runner.query_json(_verify_prompt(ear, arts), shape_hint=_VERIFY_HINT, allowed_tools=[])


async def fix_one(dir_str: str, on_log: Optional[Callable[[str], None]] = None) -> dict:
    """按某工单已写好的 _spot-check.md 确认发现，自动修复用例文字。供复核流程在复核后直接调用
    （用户要求复核完一步到位、不再单独点修复）。返回修复摘要，由复核流程并入 _spot-check.md。"""
    ear = _ear(dir_str)
    directory = Path(dir_str)
    sc = directory / "_spot-check.md"
    td = directory / "test-design.json"
    if not (sc.exists() and td.exists()):
        if on_log:
            on_log(f"{ear}: 无复核结果或无用例，跳过修复")
        return {"ear": ear, "applied": 0, "skipped": 0, "unfixable": [], "notes": "缺复核或用例"}
    arts = _arts_block(dir_str, _ARTS, cap=12000)
    r = await runner.query_json(_revise_prompt(ear, arts), shape_hint=_REVISE_HINT, allowed_tools=[])
    edits = (r or {}).get("edits") or []
    questions = (r or {}).get("questions") or []
    unfixable = (r or {}).get("unfixable") or []
    res = apply_edits(td, edits)
    res["ear"] = ear
    res["questions"] = questions      # 需人工拍板的点 → 由调用方(spot_check)折进 questions.md（可回答）
    res["unfixable"] = unfixable
    res["postcheck"] = None
    _write_md(directory, ear, res, unfixable)

    try:
        verdict = await _verify_repair(dir_str, ear)
        res["postcheck"] = verdict or {}
        res["leftover"] = _normalize_leftover(verdict or {})
        retry_edits = _leftover_text_edits(res["leftover"])
        if retry_edits:
            retry = apply_edits(td, retry_edits)
            res["postcheck_retry"] = retry
            _merge_apply_result(res, retry)
            if retry.get("applied"):
                _write_md(directory, ear, res, unfixable)
                verdict = await _verify_repair(dir_str, ear)
                res["postcheck"] = verdict or {}
                res["leftover"] = _normalize_leftover(verdict or {})
    except Exception as e:  # noqa: BLE001
        res["postcheck_error"] = f"{type(e).__name__}: {str(e)[:220]}"
        res["leftover"] = [{"finding": "修后验收失败", "fix_type": "unknown",
                            "why": "自动修复后未得到可信验收结论"}]
    _write_md(directory, ear, res, unfixable)

    if on_log:
        tail = ""
        if questions:
            tail += f"，{len(questions)} 处需人工确认（已转待确认）"
        if unfixable:
            tail += f"，{len(unfixable)} 处需重新生成/人工"
        if res.get("leftover"):
            tail += f"，{len(res['leftover'])} 处修后验收未消解"
        if res.get("postcheck_error"):
            tail += "，修后验收失败"
        if res.get("notes"):
            tail += f"（{res['notes']}）"
        on_log(f"{ear}: 自动修复 {res['applied']} 处{tail}")
    return res
