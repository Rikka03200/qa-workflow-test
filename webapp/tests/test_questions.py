"""questions.md 解析 + 算术沙箱 recompute。"""

import pytest

from webapp import config
from webapp.services import questions as q
from webapp.strong import tools


def test_parse_real_questions():
    p = config.TICKETS_DIR / "wms" / "2026-06-09" / "EAR-240444" / "questions.md"
    if not p.exists():
        pytest.skip("样本缺失")
    parsed = q.parse(p)
    assert parsed["form"] == "questions"
    assert parsed["counts"]["total"] == 2
    assert [b["num"] for b in parsed["blocks"]] == [1, 2]
    # 两题都已人工作答（A / 必填项说明）
    assert all(b["state"] == "human" for b in parsed["blocks"])
    assert parsed["blocks"][0]["answer_display"].strip() == "A"


def test_recompute_basic():
    assert tools.recompute("1+2*3") == 7
    assert tools.recompute("(10-2)//3") == 2
    assert tools.recompute("round(10/3, 2)") == 3.33
    assert tools.recompute("max(3, 7, 5)") == 7


def test_recompute_rejects_unsafe():
    with pytest.raises(tools.RecomputeError):
        tools.recompute("__import__('os').system('echo hi')")
    with pytest.raises(tools.RecomputeError):
        tools.recompute("x + 1")  # 名称引用
    with pytest.raises(tools.RecomputeError):
        tools.recompute("open('x')")  # 任意函数
    with pytest.raises(tools.RecomputeError):
        tools.recompute("10 ** 100000")  # 资源炸弹


def test_verify_claims():
    claims = tools.verify_claims("应为 3+4 = 7；但用例写成 3+4 = 8")
    assert any(c["ok"] for c in claims)
    assert any(not c["ok"] for c in claims)
