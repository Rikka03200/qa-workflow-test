"""审查确认问题的回归测试（对抗式 review 后修复点）。"""

import tempfile
from pathlib import Path

import pytest

from webapp.routers import pages
from webapp.services import tickets
from webapp.strong import resolve, tools


def test_recompute_rounding_no_false_positive():
    # 业务取整/小数四舍五入不应被判为算错（#6）
    for expr in ("17/5=3", "2/5=1", "1/3=0.33", "10/4=2.5", "7/2=4"):
        claims = tools.verify_claims(expr)
        assert claims and all(c["ok"] for c in claims), (expr, claims)
    # 真正算错仍要标记
    for expr in ("3+4=8", "10/2=6"):
        claims = tools.verify_claims(expr)
        assert claims and not claims[0]["ok"], expr


def test_safe_next_blocks_open_redirect():  # #8
    assert pages._safe_next("//evil.example") == "/"
    assert pages._safe_next("https://evil") == "/"
    assert pages._safe_next("/\\evil") == "/"
    assert pages._safe_next("/sprint/wms/2026-06-09") == "/sprint/wms/2026-06-09"


def test_path_segment_guards():  # #1/#3 基础守卫
    assert tickets.safe_product("wms") and not tickets.safe_product("../x")
    assert tickets.safe_date("2026-06-09") and not tickets.safe_date("x/../../evil")
    assert tickets.safe_ear("EAR-240444") and not tickets.safe_ear("../secret")


def _mk_questions(d: Path, body: str) -> Path:
    q = d / "questions.md"
    q.write_text(body, encoding="utf-8")
    return q


def test_resolve_writeback_matches_by_text_not_index(tmp_path):  # #2
    d = tmp_path / "EAR-999999"
    d.mkdir()
    q = _mk_questions(d,
        "# 待确认问题清单 — EAR-999999\n\n> 请在每个“✅ 答案”下方填写确认结果；如果暂时无法确认，填写 `[待确认]`。\n\n"
        "## Q1: 甲问题\n**问题**：甲\n**来源**：x\n**可能场景**：A\n**影响范围**：i\n**✅ 答案**：\n<!-- 待填 -->\n\n"
        "## Q2: 乙问题\n**问题**：乙\n**来源**：x\n**可能场景**：A\n**影响范围**：i\n**✅ 答案**：\n<!-- 待填 -->\n")
    # resolutions 顺序颠倒、无 QN 前缀 → 必须按题面文本匹配，不能按下标错配
    res = [
        {"question": "乙问题", "already_answered": False, "status": "resolved",
         "answer": "乙答", "source": "S", "reason": "", "problem": "", "possible_scenarios": [], "impact": ""},
        {"question": "甲问题", "already_answered": False, "status": "resolved",
         "answer": "甲答", "source": "S", "reason": "", "problem": "", "possible_scenarios": [], "impact": ""},
    ]
    vmap = {"existing::乙问题": {"supported": True}, "existing::甲问题": {"supported": True}}
    c = resolve.apply_writeback(q, res, [], vmap)
    out = q.read_text(encoding="utf-8")
    q1, q2 = out.split("## Q2")
    assert "甲答（据 S 自动消解）" in q1 and "乙答（据 S 自动消解）" in q2  # 未错配
    assert c["resolved_applied"] == 2


def test_resolve_writeback_skips_unmatched(tmp_path):  # #2 安全网：匹配不到不盲写
    d = tmp_path / "EAR-888888"
    d.mkdir()
    q = _mk_questions(d,
        "# 待确认问题清单 — EAR-888888\n\n> 请在每个“✅ 答案”下方填写确认结果；如果暂时无法确认，填写 `[待确认]`。\n\n"
        "## Q1: 真实问题\n**问题**：x\n**来源**：x\n**可能场景**：A\n**影响范围**：i\n**✅ 答案**：\n<!-- 待填 -->\n")
    res = [{"question": "毫不相关的问题", "already_answered": False, "status": "resolved",
            "answer": "不该写入", "source": "S", "reason": "", "problem": "", "possible_scenarios": [], "impact": ""}]
    c = resolve.apply_writeback(q, res, [], {})
    assert "不该写入" not in q.read_text(encoding="utf-8")
    assert c["resolved_applied"] == 0 and "无法定位" in c["notes"]
