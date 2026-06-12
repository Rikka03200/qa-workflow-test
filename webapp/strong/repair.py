"""结构修复（webapp 专属，写 test-design.json —— 弱模型连试多轮仍未过结构校验时的强模型兜底）。

用户选择「卡住自动升级强模型修复」：弱模型反复失败的【结构】问题，交强模型把整树重写一次，
仍由 validate-test-design 确定性闸门 + 测试点数不减护栏 + .bak + 回滚兜底——过了才落盘，
不过则保留原用例并标「需人工」。只动 json_ok==False（结构 FAIL）的单；结构合格的语义问题
归 spot_check/revise，不在此处。绝不让“修结构”变成删用例/改业务判定。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import runner, schemas
from .spot_check import _arts_block, _ear
from ..services import scripts_loader, tickets
from core.productcfg import DEFAULT_PRODUCT

_ARTS = ["requirement.md", "analysis.md", "business-context.md", "questions.md", "test-points.md"]
_REPAIR_HINT = json.dumps(schemas.REPAIR_SCHEMA, ensure_ascii=False)


def _fail_lines(directory: Path) -> list[str]:
    """该工单 test-design.json 的结构 FAIL 文案（in-process Validator，无子进程）。"""
    issues = tickets.json_structure_issues(directory)
    return [getattr(i, "message", str(i)) for i in issues if getattr(i, "level", "") == "FAIL"]


def _repair_prompt(ear: str, fails: list[str], broken_json: str, arts: str) -> str:
    return (
        f"工单 {ear} 的 test-design.json 经多轮弱模型生成后仍【未通过结构校验】。"
        f"请把它整树重写为合法的 CodeArts test-design.json——【只修结构、不改业务内容】。\n\n"
        f"【必须修复的校验失败】\n" + "\n".join(f"- {x}" for x in fails[:30]) + "\n\n"
        f"结构硬规则（CodeArts JSON 契约）：\n"
        f"- 单根树：根 text=\"{ear}\"、side=\"right\"；所有 id 为 32 位大写十六进制、全文件唯一。\n"
        f"- condition/step/expect 标记正确；每个 step 节点【恰好 1 个】expect 子节点。\n"
        f"- mark.priority 形如 {{\"1\":true}} / {{\"2\":true}}；节点 text 自包含、换行只用 <br>，"
        f"禁 <b>/<strong>/HTML 实体/反引号、禁工单号(EAR-xxx)/§/[来源:]/文件名(.md)/截图名(.png)。\n"
        f"- 平台前缀须合法（web/仓配app/采配app/零售app/POS/TMS/供应商平台/供应商app/前置仓app/TMS小程序）。\n"
        f"【内容铁律】保留所有测试点/步骤/预期的业务含义，且测试点【数量不减】；"
        f"不得借“修结构”删用例、合并用例或改写业务判定；只做结构合规化与必要的自包含化。\n\n"
        f"【当前未通过的 test-design.json】\n{broken_json}\n\n"
        f"【工单工件（供核对业务内容，已内嵌，无需读取文件）】\n{arts}\n\n"
        f"只输出修正后的完整 JSON 数组放进 test_design 字段。"
    )


def apply_repair(td_path: Path, new_tree) -> dict:
    """确定性应用整树修复：合法数组 + 测试点数不减 + 过结构校验 → .bak + 原子替换；否则放弃（原文未动）。"""
    res = {"applied": False, "notes": "", "old_testpoints": 0, "new_testpoints": 0}
    orig = td_path.read_text(encoding="utf-8")
    if not isinstance(new_tree, list) or not new_tree:
        res["notes"] = "强模型未给出合法 JSON 数组（可能输出被截断），已放弃。"
        return res
    bg = scripts_loader.batch_generate()
    try:
        old = json.loads(orig)
        res["old_testpoints"] = bg.count_stats(old).get("testpoints", 0)
    except Exception:  # noqa: BLE001
        res["old_testpoints"] = 0
    res["new_testpoints"] = bg.count_stats(new_tree).get("testpoints", 0)
    # 内容不缩水护栏：测试点数不得少于原来（防“修结构”时删用例）
    if res["old_testpoints"] and res["new_testpoints"] < res["old_testpoints"]:
        res["notes"] = (f"修复结果测试点 {res['new_testpoints']} < 原 {res['old_testpoints']}，"
                        f"疑似删用例，已放弃（用例未动）。")
        return res
    new_text = json.dumps(new_tree, ensure_ascii=False, indent=2) + "\n"
    tmp = td_path.with_name(f"{td_path.name}.{uuid.uuid4().hex}.repair.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    rc, out = bg.run_validator("validate-test-design.py", str(tmp))
    if rc != 0:
        tmp.unlink(missing_ok=True)
        res["notes"] = "强模型修复后仍未过结构校验，已放弃（用例未动）：" + str(out)[:200]
        return res
    try:
        td_path.with_name(td_path.name + ".bak").write_text(orig, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    os.replace(tmp, td_path)  # 原子替换
    res["applied"] = True
    return res


def _write_md(directory: Path, ear: str, fails: list[str], res: dict) -> None:
    L = [f"# 结构修复 — {ear}", ""]
    if res.get("applied"):
        L += [f"> 弱模型多轮未过结构校验，已由强模型整树修复并通过校验"
              f"（测试点 {res.get('old_testpoints')} → {res.get('new_testpoints')}）。", "",
              "## 已修复的结构问题"]
        L += [f"- {x}" for x in fails[:30]]
    else:
        L += [f"> 强模型结构修复未成功，已保留原用例，需人工处理。", "",
              f"说明：{res.get('notes', '')}", "", "## 仍未通过的结构问题"]
        L += [f"- {x}" for x in fails[:30]]
    (directory / "_repair.md").write_text("\n".join(L).rstrip() + "\n", encoding="utf-8")


async def repair_one(dir_str: str, on_log: Optional[Callable[[str], None]] = None) -> dict:
    ear = _ear(dir_str)
    d = Path(dir_str)
    td = d / "test-design.json"
    if not td.exists():
        return {"ear": ear, "applied": False, "skipped": "no-design"}
    fails = _fail_lines(d)
    if not fails:
        return {"ear": ear, "applied": False, "skipped": "structurally-ok"}  # 结构没坏，不修
    broken = td.read_text(encoding="utf-8")
    if len(broken) > 16000:
        broken = broken[:16000] + "\n…(已截断)"
    arts = _arts_block(dir_str, _ARTS)
    r = await runner.query_json(_repair_prompt(ear, fails, broken, arts),
                                shape_hint=_REPAIR_HINT, allowed_tools=[])
    new_tree = (r or {}).get("test_design")
    res = apply_repair(td, new_tree)
    res["ear"] = ear
    res["fails"] = len(fails)
    tickets.invalidate_badge(d)  # 改了 test-design.json，徽标缓存失效
    _write_md(d, ear, fails, res)
    if on_log:
        if res.get("applied"):
            on_log(f"{ear}: 结构修复成功（{len(fails)} 项 FAIL 已修、已过校验）")
        else:
            on_log(f"{ear}: 结构修复未成功，保留原用例并标需人工（{(res.get('notes') or '')[:80]}）")
    return res


async def run(ticket_dirs: list[str], product: str = DEFAULT_PRODUCT,
              on_log: Optional[Callable[[str], None]] = None) -> list[dict]:
    """对一批工单：结构坏的(json_ok==False)逐单强模型修复，结构好的跳过。供定稿后自动兜底调用。"""
    if not ticket_dirs:
        return []
    results = await asyncio.gather(*[repair_one(d, on_log) for d in ticket_dirs],
                                   return_exceptions=True)
    if on_log:
        for r in results:
            if isinstance(r, Exception):
                on_log(f"结构修复某单出错（已跳过）：{type(r).__name__}: {str(r)[:160]}")
    return [r for r in results if isinstance(r, dict)]
