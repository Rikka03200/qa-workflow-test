"""test-design.json 服务：只读树渲染模型 + JSONPath 校验标记映射 + raw 字节保存。

铁律（来自 plan + CLAUDE.md §3.1）：
- 节点类型按 marker key 推断（container=只有 children；test-point=有 mark+testPoint；
  叶子 condition/step/expect）。
- raw 保存【写原文字节】，仅做一次非破坏性 json.loads 合法性检查，**绝不**经 Python
  对象 round-trip（防 key 重排/转义漂移破坏粘进 CodeArts 的字节一致性）。
- FAIL 阻断保存；WARN 放行。结构化增改仍交回 CodeArts。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from . import artifacts, scripts_loader, tickets

_SEV_RANK = {"FAIL": 2, "WARN": 1, "": 0, None: 0}


def _node_type(node: dict, is_root: bool) -> str:
    if is_root:
        return "root"
    if "mark" in node or "testPoint" in node:
        return "testpoint"
    if node.get("condition") == "Y":
        return "condition"
    if node.get("step") == "Y":
        return "step"
    if node.get("expect") == "Y":
        return "expect"
    return "container"


def _priority(node: dict):
    mark = node.get("mark")
    if isinstance(mark, dict) and isinstance(mark.get("priority"), dict):
        for k in ("1", "2", "3"):
            if mark["priority"].get(k) is True:
                return k
    return None


def _anchor(jsonpath: str) -> str:
    return "node-" + re.sub(r"[^0-9a-zA-Z]+", "-", jsonpath).strip("-")


def _build(node, jsonpath: str, is_root: bool, registry: dict) -> dict:
    rendered = {
        "id": node.get("id") if isinstance(node, dict) else None,
        "text": node.get("text", "") if isinstance(node, dict) else str(node),
        "jsonpath": jsonpath,
        "anchor": _anchor(jsonpath),
        "type": _node_type(node, is_root) if isinstance(node, dict) else "unknown",
        "priority": _priority(node) if isinstance(node, dict) else None,
        "issues": [],
        "children": [],
        "severity": "",
    }
    registry[jsonpath] = rendered
    if isinstance(node, dict):
        children = node.get("children")
        if isinstance(children, list):
            for i, child in enumerate(children):
                rendered["children"].append(
                    _build(child, f"{jsonpath}.children[{i}]", False, registry))
    return rendered


def _propagate_severity(node: dict) -> str:
    sev = max((_SEV_RANK.get(i["level"], 0) for i in node["issues"]), default=0)
    for c in node["children"]:
        sev = max(sev, _SEV_RANK.get(_propagate_severity(c), 0))
    node["severity"] = "FAIL" if sev == 2 else "WARN" if sev == 1 else ""
    return node["severity"]


def _resolve_issue_node(path: str, registry: dict) -> dict | None:
    p = path
    while p:
        if p in registry:
            return registry[p]
        if "." in p:
            p = p.rsplit(".", 1)[0]
        else:
            break
    return registry.get("$[0]")


def load(path: Path) -> dict:
    """解析 test-design.json → 渲染树 + 内联校验标记。"""
    if not path.exists():
        return {"ok": False, "error": "test-design.json 不存在", "exists": False}
    raw = path.read_text(encoding="utf-8")
    result = {"exists": True, "raw": raw, "mtime_ns": path.stat().st_mtime_ns}

    # 校验（in-process Validator）
    vtd = scripts_loader.validate_test_design()
    try:
        issues = vtd.Validator(path).validate()
    except Exception as e:  # noqa: BLE001
        issues = []
        result["validator_error"] = str(e)
    issue_dicts = [{"level": i.level, "path": i.path, "message": i.message} for i in issues]
    result["issues"] = issue_dicts
    result["fail_count"] = sum(1 for i in issue_dicts if i["level"] == "FAIL")
    result["warn_count"] = sum(1 for i in issue_dicts if i["level"] == "WARN")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        result["ok"] = False
        result["error"] = f"JSON 解析失败：第 {e.lineno} 行第 {e.colno} 列，{e.msg}"
        result["tree"] = None
        return result

    if not isinstance(data, list) or not data:
        result["ok"] = False
        result["error"] = "根结构必须是非空数组"
        result["tree"] = None
        return result

    registry: dict[str, dict] = {}
    tree = _build(data[0], "$[0]", True, registry)

    file_issues = []
    for i in issue_dicts:
        if i["path"] in ("$", "$[0]") and i["path"] == "$":
            file_issues.append(i)
            continue
        target = _resolve_issue_node(i["path"], registry)
        if target is not None:
            target["issues"].append(i)
        else:
            file_issues.append(i)
    _propagate_severity(tree)

    result["ok"] = True
    result["tree"] = tree
    result["file_issues"] = file_issues
    # 供「问题清单」侧栏点击定位
    result["issue_anchors"] = [
        {**i, "anchor": (_resolve_issue_node(i["path"], registry) or {}).get("anchor", "")}
        for i in issue_dicts
    ]
    return result


def validate_text(path: Path, text: str) -> dict:
    """不落盘预校验 raw（写同目录唯一临时文件 → Validator → 删）。"""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.precheck.tmp")
    try:
        json.loads(text)  # 合法性（不 round-trip）
    except json.JSONDecodeError as e:
        return {"ok": False, "json_error": f"第 {e.lineno} 行第 {e.colno} 列，{e.msg}", "issues": []}
    tmp.write_text(text, encoding="utf-8")
    try:
        vtd = scripts_loader.validate_test_design()
        issues = vtd.Validator(tmp).validate()
        ds = [{"level": i.level, "path": i.path, "message": i.message} for i in issues]
        return {"ok": all(i["level"] != "FAIL" for i in ds), "issues": ds}
    finally:
        tmp.unlink(missing_ok=True)


def save_raw(path: Path, text: str, client_mtime_ns: int) -> dict:
    """raw 字节保存：合法性检查 + Validator(FAIL 阻断) + 乐观并发 + 原子写原文字节。"""
    if path.exists() and client_mtime_ns and path.stat().st_mtime_ns != client_mtime_ns:
        return {"ok": False, "kind": "conflict", "error": "文件已被更新，请刷新后重试。"}
    try:
        json.loads(text)  # 仅合法性；不 round-trip、不重序列化
    except json.JSONDecodeError as e:
        return {"ok": False, "kind": "json", "error": f"JSON 非法：第 {e.lineno} 行第 {e.colno} 列，{e.msg}"}

    # 规范 EOF：单个换行结尾（与契约 json.dumps(...)+"\n" 一致），不动 JSON 内容字节
    body = text.rstrip("\n") + "\n"
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(body, encoding="utf-8")

    vtd = scripts_loader.validate_test_design()
    issues = vtd.Validator(tmp).validate()
    fails = [i for i in issues if i.level == "FAIL"]
    if fails:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "kind": "validation", "error": "结构校验未通过（未落盘）。",
                "issues": [{"level": i.level, "path": i.path, "message": i.message} for i in issues]}
    if path.exists() and client_mtime_ns and path.stat().st_mtime_ns != client_mtime_ns:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "kind": "conflict", "error": "文件刚被更新，请刷新后重试。"}
    os.replace(tmp, path)
    tickets.invalidate_badge(path.parent)
    artifacts.mirror_file(path)
    warns = [{"level": i.level, "path": i.path, "message": i.message} for i in issues if i.level == "WARN"]
    return {"ok": True, "issues": warns}
