"""知识回填 kb_extract.apply 的共享 rules.md 写入回归：建章节+§0、续号复用、toc 不过回滚、空规则。

用真实 scripts/kb-check-toc.py 校验；monkeypatch rules_path 指向 tmp，绝不碰真知识库。
"""

from webapp import config
from webapp.strong import kb_extract

VALID = """# WMS 业务规则

## 0. 目录索引

- §1 基础规则

## 1. 基础规则

这是基础规则。
"""


def test_apply_creates_chapter_and_toc_ref(tmp_path, monkeypatch):
    rp = tmp_path / "rules.md"
    rp.write_text(VALID, encoding="utf-8")
    monkeypatch.setattr(kb_extract, "rules_path", lambda product: rp)
    rules = [{"title": "查询单位表头联动",
              "content": "切换查询单位后，列表头随之变化、数值同步换算。",
              "source": "本工单修改方案：原句"}]
    res = kb_extract.apply("wms", "EAR-1", rules, date="2026-06-08")
    assert res["ok"] is True, res
    assert res["applied"] == 1
    text = rp.read_text(encoding="utf-8")
    assert "## 2. 知识回填" in text                       # 新建专属章节（章节号续在最大之后）
    assert "§2 知识回填" in text                           # §0 目录引用（toc 通过的关键）
    assert "### 2.1 查询单位表头联动" in text              # 规则以 ### 小节累积
    assert "本工单修改方案：原句" in text                  # 来源溯源
    assert "EAR-1 · 2026-06-08" in text                    # 工单+日期
    assert (tmp_path / "rules.md.bak").exists()            # 原知识库已备份


def test_apply_second_reuses_chapter_and_increments(tmp_path, monkeypatch):
    rp = tmp_path / "rules.md"
    rp.write_text(VALID, encoding="utf-8")
    monkeypatch.setattr(kb_extract, "rules_path", lambda product: rp)
    kb_extract.apply("wms", "EAR-1", [{"title": "A", "content": "规则A。", "source": "本工单修改方案：x"}], date="2026-06-08")
    r2 = kb_extract.apply("wms", "EAR-2", [{"title": "B", "content": "规则B。", "source": "本工单修改方案：y"}], date="2026-06-08")
    assert r2["ok"] is True
    text = rp.read_text(encoding="utf-8")
    assert text.count("## 2. 知识回填") == 1                # 只建一个回填章节，复用
    assert "### 2.1 A" in text and "### 2.2 B" in text      # 小节续号


def test_apply_rollback_when_toc_fails(tmp_path, monkeypatch):
    rp = tmp_path / "rules.md"
    bad = "# 没有 §0 目录的规则库\n\n## 1. 基础\n\n规则。\n"   # 无 §0 → toc FATAL → 必须回滚
    rp.write_text(bad, encoding="utf-8")
    monkeypatch.setattr(kb_extract, "rules_path", lambda product: rp)
    res = kb_extract.apply("wms", "EAR-1", [{"title": "A", "content": "规则A。", "source": "x"}])
    assert res["ok"] is False
    assert "目录校验" in res["notes"]
    assert rp.read_text(encoding="utf-8") == bad            # 校验不过：知识库一字不动
    assert not (tmp_path / "rules.md.bak").exists()


def test_apply_no_selection():
    res = kb_extract.apply("wms", "EAR-1", [])
    assert res["ok"] is False and res["applied"] == 0


def test_apply_uses_db_store_for_real_rules_path(monkeypatch):
    calls = []

    class FakeStore:
        def append_backfill_rules(self, product, ear, rules, date):
            calls.append((product, ear, rules, date))
            return {"ok": True, "applied": len(rules), "chapter": 247, "notes": ""}

    class FakeLoader:
        @staticmethod
        def load_normal(name):
            assert name == "kb_store"
            return FakeStore()

    real_path = config.REPO_ROOT / "_kb" / "projects" / "wms" / "rules.md"
    monkeypatch.setattr(kb_extract, "rules_path", lambda product: real_path)
    monkeypatch.setattr("webapp.services.scripts_loader.load_normal", FakeLoader.load_normal)

    rules = [{"title": "A", "content": "规则A", "source": "方案"}]
    res = kb_extract.apply("wms", "EAR-1", rules, date="2026-06-10")

    assert res == {"ok": True, "applied": 1, "chapter": 247, "notes": ""}
    assert calls == [("wms", "EAR-1", rules, "2026-06-10")]
