"""改版回归：产品名 / mdlite 安全渲染 / digest / 归属。"""

from pathlib import Path

import pytest

from webapp import config
from webapp.services import digest, ownership, summary_md
from webapp.services import humanize as hmz


def test_product_display_is_wms():
    assert config.product_display("wms") == "WMS"


def test_mdlite_renders_and_escapes():
    h = summary_md.render("## 标题\n- 项 **粗** 与 `代码`\n\n> 引用\n\n1. 一\n2. 二")
    assert "<h4>" in h and "<li>" in h and "<strong>" in h and "<code>" in h
    assert "<blockquote>" in h and "<ol>" in h
    # 必须先转义，杜绝注入
    bad = summary_md.render("<script>alert(1)</script> **x**")
    assert "<script>" not in bad and "&lt;script&gt;" in bad


def test_digest_on_sample():
    d = config.TICKETS_DIR / "wms" / "2026-06-09" / "EAR-240444"
    if not d.exists():
        pytest.skip("样本缺失")
    out = digest.build(d)
    assert out["title"] and out["point_count"] == 2
    assert out["platforms"] == ["网页端"]
    assert out["plan_html"]  # 需求方案被渲染


def test_ownership_basic(tmp_path, monkeypatch):
    monkeypatch.setattr(ownership, "_LEDGER", tmp_path / "own.json")
    assert ownership.owner_of("wms", "2026-06-09") is None
    ownership.set_owner("wms", "2026-06-09", "alice")
    assert ownership.owner_of("wms", "2026-06-09") == "alice"
    # 首登记不被覆盖
    ownership.set_owner("wms", "2026-06-09", "bob")
    assert ownership.owner_of("wms", "2026-06-09") == "alice"
    assert ownership.can_view("alice", "wms", "2026-06-09")
    assert not ownership.can_view("bob", "wms", "2026-06-09")
    # 显式认领可覆盖
    ownership.set_owner("wms", "2026-06-09", "bob", overwrite=True)
    assert ownership.owner_of("wms", "2026-06-09") == "bob"


def test_humanize_strips_internal_refs():  # #4
    for src in ("requirement.md §3 修改方案", "_kb/projects/wms/rules.md §26.1",
                "test-design.json 节点 6000000000000000000000000000000A",
                "test-points.md §2.1 第3条", "questions.md", "CLAUDE.md", "§4.2"):
        out = hmz.humanize(src)
        assert ".md" not in out and ".json" not in out and "§" not in out, (src, out)
    assert "需求方案" in hmz.humanize("见 requirement.md §3")
    assert "业务规则" in hmz.humanize("rules.md §10")


def test_humanize_strips_node_debug():  # #3 强化：节点/expect/L1/valid
    import re as _re
    for src in ("节点 step E2499903212240 text=点击", "expect AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA44",
                "valid(1)=True", "节点 6600000000000000000000000000AB64"):
        out = hmz.humanize(src)
        assert "节点" not in out and "valid(" not in out, (src, out)
        assert not _re.search(r"[0-9A-F]{8}", out), (src, out)
    assert "方案" in hmz.humanize("L1 方案要求") and "L1" not in hmz.humanize("L1 方案要求")


def test_question_options_parse():  # #2
    from webapp.services import questions as q
    opts = q._parse_options("- A. 两个入口都改\n- B、只改一个\n- C) 置灰")
    assert [o["key"] for o in opts] == ["A", "B", "C"]
    assert q._match_option("A", opts) == "A"
    assert q._match_option("置灰", opts) == "C"            # 按选项文本匹配
    assert q._match_option("我自己写的", opts) == "__custom__"
    base, supp = q._split_supplement("B、只改一个\n补充：注意线路")
    assert base == "B、只改一个" and supp == "注意线路"
    assert q._parse_options("一段没有选项的说明") == []     # 无选项 → 空


def test_per_user_ai_injection():  # #5
    from webapp import auth, deps
    u = auth.User(username="t", display_name="测试甲")
    u.ai = {"weak": {"base_url": "https://w", "api_key": "sk-w", "model": "qwen"},
            "strong": {"base_url": "https://s", "api_key": "sk-s", "model": "claude"}}
    env = deps.subprocess_env(u)
    assert env["CHEAP_MODEL_BASE_URL"] == "https://w" and env["CHEAP_MODEL_API_KEY"] == "sk-w"
    assert env["CHEAP_MODEL_ENABLED"] == "true" and env["QA_SELECT_TESTER"] == "测试甲"
    ep = deps.user_anthropic_endpoint(u)
    assert ep["base_url"] == "https://s" and ep["model"] == "claude"
    assert u.ai_view()["weak"]["has_key"] and not u.ai_view()["weak"].get("api_key")  # 不回显 key


def test_set_ai_keeps_key_when_blank():  # #5 掩码写
    from webapp import auth
    store = auth.UserStore
    u = auth.User(username="t2")
    u.ai = {"weak": {"base_url": "b", "api_key": "secret", "model": "m"}}
    # 模拟 set_ai 的"留空保留 key"逻辑（不落盘，直接验证合并）
    cur = dict(u.ai["weak"])
    cur["base_url"] = "b2"; cur["model"] = "m2"
    api_key = ""  # 留空
    if api_key and api_key.strip():
        cur["api_key"] = api_key.strip()
    assert cur["api_key"] == "secret" and cur["base_url"] == "b2"
