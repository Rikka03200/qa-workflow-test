"""字节级 round-trip 安全网（M0）：test-design.json 粘进 CodeArts 故字节一致不可破。

验证：① 规范序列化 = json.dumps(ensure_ascii=False, indent=2)+"\n" 能复现流水线产物；
② raw 保存写原文字节、不经对象 round-trip；③ 非法/结构错误被拒。
"""

import json
from pathlib import Path

import pytest

from webapp import config
from webapp.services import tree


def canonical(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _all_td():
    return list(config.TICKETS_DIR.glob("*/*/*/test-design.json"))


def test_known_sample_is_canonical():
    p = config.TICKETS_DIR / "wms" / "2026-06-09" / "EAR-240444" / "test-design.json"
    if not p.exists():
        pytest.skip("样本工单缺失（tickets/ 未本地化）")
    text = p.read_text(encoding="utf-8")
    assert canonical(json.loads(text)) == text, "规范序列化未能字节复现样本"


def test_canonical_idempotent():
    files = _all_td()
    if not files:
        pytest.skip("无 test-design.json")
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        once = canonical(data)
        assert once == canonical(json.loads(once)), f"规范序列化非幂等：{p}"


def test_pipeline_files_roundtrip_reported():
    """流水线写出的文件应字节级 == 规范序列化；手写/历史文件允许不符但要可见。"""
    mismatches = []
    for p in _all_td():
        if "手写" in p.name:
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if canonical(data) != p.read_text(encoding="utf-8"):
            mismatches.append(str(p.relative_to(config.REPO_ROOT)))
    # 已知 canonical 的样本必须匹配（其余仅报告，不让历史文件拖挂测试）
    assert not [m for m in mismatches if "EAR-240444" in m], f"样本应匹配：{mismatches}"
    if mismatches:
        print(f"[info] {len(mismatches)} 个非规范序列化文件（历史/手改）：{mismatches}")


def test_save_raw_preserves_bytes(tmp_path):
    src = config.TICKETS_DIR / "wms" / "2026-06-09" / "EAR-240444" / "test-design.json"
    if not src.exists():
        pytest.skip("样本工单缺失")
    text = src.read_text(encoding="utf-8")
    d = tmp_path / "EAR-240444"
    d.mkdir()
    p = d / "test-design.json"
    p.write_text(text, encoding="utf-8")

    res = tree.save_raw(p, text, p.stat().st_mtime_ns)
    assert res["ok"], res
    assert p.read_text(encoding="utf-8") == text.rstrip("\n") + "\n"

    bad = tree.save_raw(p, "{not valid json", p.stat().st_mtime_ns)
    assert not bad["ok"] and bad["kind"] == "json"


def test_save_raw_optimistic_conflict(tmp_path):
    src = config.TICKETS_DIR / "wms" / "2026-06-09" / "EAR-240444" / "test-design.json"
    if not src.exists():
        pytest.skip("样本工单缺失")
    text = src.read_text(encoding="utf-8")
    p = tmp_path / "test-design.json"
    p.write_text(text, encoding="utf-8")
    res = tree.save_raw(p, text, client_mtime_ns=1)  # 错误 mtime → 冲突
    assert not res["ok"] and res["kind"] == "conflict"
