#!/usr/bin/env python
"""
scripts/validate-containers.py

容器路径确定性校验：把 test-design.json 里每个容器节点的父子关系，
对照 modules.md §2 CodeArts 用例存放目录树校验。
捕获两类弱模型高发错误：① 自创不存在的容器节点；② 把节点错挂到非法父级
（例：把“波次策略”挂到“出库操作”下而非“业务规则”下）。

§2 同名节点出现在多个分支时（如“波次策略”），只要 test-design 里的父子边
在 §2 任一处成立即视为合法。

用法：
  python scripts/validate-containers.py tickets/wms/2026-05-12/EAR-240883/test-design.json --product wms
  python scripts/validate-containers.py <ticket-dir-or-json> [--product wms] [--quiet]
退出码：0=全部合法；1=有非法容器边；2=找不到文件/树解析失败。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.productcfg import DEFAULT_PRODUCT
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import kb_store  # type: ignore
except Exception:  # noqa: BLE001
    kb_store = None

TREE_LINE = re.compile(r"^([│\s]*)[├└]─+\s*(.+?)\s*$")
MARKER_KEYS = ("mark", "testPoint", "step", "expect", "condition")


def parse_modules_tree(product: str) -> tuple[set[tuple[str, str]], set[str]]:
    """解析 modules.md §2 ASCII 树。返回 (合法父子边集合, 合法顶层平台名集合)。"""
    mpath = REPO_ROOT / "_kb" / "projects" / product / "modules.md"
    rel = mpath.relative_to(REPO_ROOT).as_posix()
    md = ""
    if kb_store is not None:
        md = kb_store.read_text(rel, mpath)
    if not md and mpath.exists():
        md = mpath.read_text(encoding="utf-8")
    if not md:
        raise FileNotFoundError(f"未找到 {rel}（产品 KB 未初始化？）")
    # 锚定 §2 标题行（容忍 “## 2.” / “## 2 ” 等写法）；若缺失必须报错，绝不退化成“抓全文第一个代码块”
    parts = re.split(r"(?m)^##\s*2[.\s]", md)
    if len(parts) < 2:
        raise ValueError("modules.md 未找到 §2 标题（形如 “## 2. …”）；无法确定容器路径真源")
    # 限制在 §2 区间（到下一个 “## ” 二级标题前），避免抓到 §3/§2.x 的无关代码块
    after = re.split(r"(?m)^##\s", parts[1])[0]
    blocks = re.findall(r"```(?:text)?\s*(.*?)```", after, re.S)
    if not blocks:
        raise ValueError("modules.md §2 区间内未找到目录树代码块")
    tree = blocks[0]
    edges: set[tuple[str, str]] = set()
    roots: set[str] = set()
    stack: list[tuple[int, str]] = []  # (depth, name)
    for raw in tree.splitlines():
        m = TREE_LINE.match(raw)
        if not m:
            continue
        indent, name = m.group(1), m.group(2).strip()
        depth = len(indent) // 3  # 每层约 3 字符（“│  ” 或三空格）
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            edges.add((stack[-1][1], name))
        else:
            roots.add(name)
        stack.append((depth, name))
    return edges, roots


def is_container(node: dict) -> bool:
    return isinstance(node, dict) and not any(k in node for k in MARKER_KEYS)


def collect_container_edges(data) -> tuple[list[tuple[str, str]], list[str]]:
    """返回 (容器父子边列表, 顶层平台名列表)。根=工单号，其直接子节点视为平台层。"""
    edges: list[tuple[str, str]] = []
    platforms: list[str] = []
    root = data[0] if isinstance(data, list) and data else data
    if not isinstance(root, dict):
        # 根非对象（如 [] 或 ["x"]）属模型输出结构问题，交给 validate-test-design 报可喂回的 FAIL；
        # 这里不重复判定、也不崩溃，返回空集（容器校验对其无可判定）。
        return [], []
    for plat in root.get("children", []) or []:
        if is_container(plat):
            platforms.append(plat.get("text", ""))
            _walk(plat, edges)
    return edges, platforms


def _walk(node: dict, edges: list[tuple[str, str]]) -> None:
    pname = node.get("text", "")
    for c in node.get("children", []) or []:
        # 所有容器节点都记父子边（含无 children 的空占位容器，以便捕获错挂）；仅在有子节点时递归
        if isinstance(c, dict) and is_container(c):
            edges.append((pname, c.get("text", "")))
            if c.get("children"):
                _walk(c, edges)


def main() -> int:
    ap = argparse.ArgumentParser(description="容器路径对照 modules.md §2 校验")
    ap.add_argument("path", help="test-design.json 或工单目录")
    ap.add_argument("--product", default=DEFAULT_PRODUCT)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    p = Path(a.path)
    if p.is_dir():
        p = p / "test-design.json"
    if not p.exists():
        print(f"FAIL: 文件不存在 {p}")
        return 2
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"FAIL: JSON 解析失败 {e}")
        return 2

    try:
        valid_edges, valid_roots = parse_modules_tree(a.product)
        edges, platforms = collect_container_edges(data)
    except Exception as e:
        # 缺 modules.md / 缺 §2 / 根非对象 等基础设施问题 → 退出码 2（契约：找不到文件/树解析失败）
        print(f"FAIL: 容器树/边解析失败：{e}")
        return 2

    # 两类问题分级（按置信度区分严重程度）：
    #   错挂  child 存在于 §2 但挂到了非法父级 → FAIL：高置信，喂回弱模型重生成
    #   未收录 child 不在 §2 任何位置        → WARN：§2.2 承认 仓配app 等二级菜单
    #          “暂未抓全、按需补”，可能是自创、也可能是真实但未录入——交人工/强模型
    #          裁定（补进 §2 或修正路径），不硬阻断生成
    fails: list[str] = []
    warns: list[str] = []
    for plat in platforms:
        if plat not in valid_roots:
            fails.append(f"FAIL: 顶层平台节点 “{plat}” 不在 modules.md §2 根层 {sorted(valid_roots)}")
    for parent, child in edges:
        if (parent, child) not in valid_edges:
            # 给出诊断：child 在 §2 里的合法父级
            legit_parents = sorted({pp for (pp, cc) in valid_edges if cc == child})
            if legit_parents:
                fails.append(f"FAIL: 容器错挂 “{parent} → {child}”；§2 中 “{child}” 的合法父级为 {legit_parents}")
            else:
                warns.append(
                    f"WARN: 容器节点 “{child}”（父级 “{parent}”）不在 modules.md §2——"
                    f"可能是自创，或 §2 尚未收录该二级菜单（见 §2.2「按需补」）；"
                    f"请人工确认：补进 §2 或修正容器路径"
                )

    if not a.quiet:
        print(f"=== {p.relative_to(REPO_ROOT) if REPO_ROOT in p.parents else p} ===")
        print(f"平台层：{platforms}；容器边 {len(edges)} 条")
    for w in warns:
        print(w)
    if fails:
        for f in fails:
            print(f)
        if not a.quiet:
            print(f"\n=== 汇总 === 容器错挂 {len(fails)} 项（FAIL）；未收录 {len(warns)} 项（WARN）")
        return 1
    if warns:
        if not a.quiet:
            print(f"\n=== 汇总 === 无错挂；未收录 {len(warns)} 项（WARN，需人工确认，不阻断）")
        return 0
    print("PASS: 所有容器节点路径均符合 modules.md §2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
