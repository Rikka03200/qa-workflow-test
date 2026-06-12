#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kb-check-toc.py — 检查 _kb/projects/<product>/rules.md 与其 §0 目录索引是否同步。

用法：
  python scripts/kb-check-toc.py                       # 默认检查 wms
  python scripts/kb-check-toc.py --product wms
  python scripts/kb-check-toc.py --path PATH/rules.md  # 直接指定文件

退出码：
  0 = 完全同步
  1 = 检测到脱节（游离章节 / 死引用 / 客户定制速查表不匹配）
"""

from __future__ import annotations
import argparse
import re
import sys
import io
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.productcfg import DEFAULT_PRODUCT

# Windows 控制台 UTF-8
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

CHAPTER_RE = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$", re.MULTILINE)  # ## N. 标题
TOC_REF_RE = re.compile(r"§(\d+)(?!\.\d)")  # §N 但不是 §N.M（节级引用单独处理）
TOC_HEADER_RE = re.compile(r"^##\s+0\.\s+目录索引", re.MULTILINE)
TOC_END_RE = re.compile(r"^##\s+1\.\s+", re.MULTILINE)  # §0 区段到 §1 之前
CUSTOM_TABLE_RE = re.compile(r"§0\.16\s*客户定制专题速查", re.IGNORECASE)
ONLY_CUSTOM_RE = re.compile(r"仅\s*(\d{4,6})\s*[^\n（(]{0,16}?(?:启用|开放|定制)")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--product", default=DEFAULT_PRODUCT, help=f"产品代号（默认 {DEFAULT_PRODUCT}）")
    ap.add_argument("--path", default=None, help="直接指定 rules.md 路径（覆盖 --product）")
    ap.add_argument("--strict-custom", action="store_true",
                    help="严格模式：§0.16 客户定制速查表必须覆盖所有节级 '仅 XXXX 启用' 章节")
    return ap.parse_args()


def load_rules_md(args) -> tuple[Path, str]:
    if args.path:
        p = Path(args.path)
    else:
        p = REPO_ROOT / "_kb" / "projects" / args.product / "rules.md"
    if not p.exists():
        print(f"ERROR: 文件不存在 {p}", file=sys.stderr)
        sys.exit(2)
    return p, p.read_text(encoding="utf-8")


def find_toc_region(text: str) -> tuple[int, int] | None:
    m1 = TOC_HEADER_RE.search(text)
    if not m1:
        return None
    m2 = TOC_END_RE.search(text, m1.end())
    if not m2:
        return None
    return (m1.start(), m2.start())


def main() -> int:
    args = parse_args()
    path, text = load_rules_md(args)

    # 1. 所有主章节
    chapters: dict[int, str] = {}
    for m in CHAPTER_RE.finditer(text):
        n = int(m.group(1))
        if n == 0:
            continue
        chapters[n] = m.group(2)

    # 2. §0 区段
    region = find_toc_region(text)
    if region is None:
        print("FATAL: 未找到 §0 目录索引区段（或未找到 §1 终止锚）")
        return 1
    toc_text = text[region[0]:region[1]]

    # 3. §0 引用集合
    referenced = {int(x) for x in TOC_REF_RE.findall(toc_text)}

    # 4. 差集
    chapter_set = set(chapters.keys())
    orphans = sorted(chapter_set - referenced)                # 章节存在 / §0 未引
    dead = sorted(referenced - chapter_set - {0})             # §0 引用 / 章节不存在；§0 是 TOC 自身不计

    # 5. 客户定制速查表 vs 正文 "仅 XXXX 启用"
    custom_chapters: dict[int, set[str]] = {}
    current_n = 0
    for line in text.splitlines():
        cm = CHAPTER_RE.match(line)
        if cm:
            current_n = int(cm.group(1))
            continue
        if current_n == 0:
            continue
        for ocm in ONLY_CUSTOM_RE.finditer(line):
            custom_chapters.setdefault(current_n, set()).add(ocm.group(1))

    custom_table = ""
    cm_table = CUSTOM_TABLE_RE.search(toc_text)
    if cm_table:
        custom_table = toc_text[cm_table.start():]
    table_accounts = set(re.findall(r"\b(\d{4,6})\b", custom_table))

    custom_missing: list[tuple[int, str, str]] = []  # (章节号, 账套, 章节标题)
    for ch_n, accounts in sorted(custom_chapters.items()):
        for acct in accounts:
            if acct not in table_accounts:
                custom_missing.append((ch_n, acct, chapters.get(ch_n, "?")))

    # 6. 报告
    total_chapters = len(chapters)
    referenced_in_toc = len(referenced & chapter_set)
    print(f"=== rules.md TOC 同步检查报告 ===")
    print(f"文件：{path}")
    print(f"主章节总数：{total_chapters}")
    print(f"§0 已引用章节数：{referenced_in_toc} / {total_chapters}")
    print()

    issues = 0

    if orphans:
        issues += 1
        print(f"❌ 游离章节（{len(orphans)} 个）— 章节存在但 §0 未引用：")
        for n in orphans:
            print(f"   §{n} {chapters[n]}")
        print()

    if dead:
        issues += 1
        print(f"❌ 死引用（{len(dead)} 个）— §0 引用了不存在的章节号：")
        for n in dead:
            print(f"   §{n}")
        print()

    if custom_missing and args.strict_custom:
        issues += 1
        print(f"⚠ §0.16 客户定制速查表可能漏列（{len(custom_missing)} 处）：")
        for n, acct, title in custom_missing:
            print(f"   §{n} {title} → 账套 {acct} 未在 §0.16 表中")
        print()
    elif custom_missing:
        print(f"ℹ §0.16 客户定制速查表可能漏列 {len(custom_missing)} 处（用 --strict-custom 标为错误）")
        print()

    if issues == 0:
        print("✅ §0 同步完整")
        return 0
    else:
        print(f"=== 共 {issues} 类问题待修复 ===")
        return 1


if __name__ == "__main__":
    sys.exit(main())
