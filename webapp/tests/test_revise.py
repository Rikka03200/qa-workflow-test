"""复核后修复 revise.apply_edits 的确定性回归：按 id 改文本 / 护栏跳过 / 校验不过回滚。"""

import json

from webapp.services import scripts_loader
from webapp.strong import revise


def _tree():
    a, b, c = "A" * 32, "B" * 32, "C" * 32
    return [{"id": a, "text": "根", "children": [
        {"id": b, "text": "原预期：表头名称保持不变", "mark": {}},
        {"id": c, "text": "另一节点", "mark": {}},
    ]}], (a, b, c)


def test_index_by_id_walks_tree():
    data, (a, b, c) = _tree()
    idx = revise._index_by_id(data)
    assert set(idx) == {a, b, c}
    assert idx[b]["text"].startswith("原预期")


def _patch_validator(monkeypatch, rc, out=""):
    mod = scripts_loader.batch_generate()
    monkeypatch.setattr(mod, "run_validator", lambda name, p: (rc, out))


def test_apply_edits_replaces_text_and_backs_up(tmp_path, monkeypatch):
    _patch_validator(monkeypatch, 0)
    data, (a, b, c) = _tree()
    td = tmp_path / "test-design.json"
    orig = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    td.write_text(orig, encoding="utf-8")
    res = revise.apply_edits(td, [{"node_id": b, "new_text": "预期：表头随所选单位变化<br>数值同步换算"}])
    assert res["applied"] == 1 and res["skipped"] == 0
    after = json.loads(td.read_text(encoding="utf-8"))
    nb = revise._index_by_id(after)[b]
    assert nb["text"] == "预期：表头随所选单位变化<br>数值同步换算"
    assert revise._index_by_id(after)[c]["text"] == "另一节点"      # 其余节点不动
    assert (tmp_path / "test-design.json.bak").read_text(encoding="utf-8") == orig  # 原文备份可回退


def test_apply_edits_forbidden_ref_skipped(tmp_path, monkeypatch):
    _patch_validator(monkeypatch, 0)
    data, (a, b, c) = _tree()
    td = tmp_path / "test-design.json"
    orig = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    td.write_text(orig, encoding="utf-8")
    # 新文本混入工单号 → 护栏跳过、不应用、文件不动
    res = revise.apply_edits(td, [{"node_id": b, "new_text": "见 EAR-252916 评论"}])
    assert res["applied"] == 0 and res["skipped"] == 1
    assert td.read_text(encoding="utf-8") == orig
    assert not (tmp_path / "test-design.json.bak").exists()


def test_apply_edits_rollback_on_validation_fail(tmp_path, monkeypatch):
    _patch_validator(monkeypatch, 1, "结构校验失败：marker 缺失")
    data, (a, b, c) = _tree()
    td = tmp_path / "test-design.json"
    orig = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    td.write_text(orig, encoding="utf-8")
    res = revise.apply_edits(td, [{"node_id": b, "new_text": "新预期 X"}])
    assert res["applied"] == 0
    assert "校验未通过" in res["notes"]
    assert td.read_text(encoding="utf-8") == orig                  # 校验不过：原文一字未动


def test_apply_edits_unknown_node_skipped(tmp_path, monkeypatch):
    _patch_validator(monkeypatch, 0)
    data, (a, b, c) = _tree()
    td = tmp_path / "test-design.json"
    td.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    res = revise.apply_edits(td, [{"node_id": "Z" * 32, "new_text": "改不存在的节点"}])
    assert res["applied"] == 0 and res["skipped"] == 1


def test_apply_edits_rejects_new_daiqueren_marker(tmp_path, monkeypatch):
    """复核绝不往用例里新增 [待确认] 图章（死胡同、用户无法回答）——护栏跳过该处、文件不动。
    需人工的点应走 revise 的 questions 通道折进 questions.md，而非塞进用例文字。"""
    _patch_validator(monkeypatch, 0)
    data, (a, b, c) = _tree()
    td = tmp_path / "test-design.json"
    orig = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    td.write_text(orig, encoding="utf-8")
    res = revise.apply_edits(td, [{"node_id": b, "new_text": "预期：[待确认: 方案未明确商品可见范围]"}])
    assert res["applied"] == 0 and res["skipped"] == 1          # 含 [待确认 → 护栏拦下
    assert td.read_text(encoding="utf-8") == orig               # 用例一字未动
    assert not (tmp_path / "test-design.json.bak").exists()
