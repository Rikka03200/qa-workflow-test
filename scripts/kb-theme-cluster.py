#!/usr/bin/env python
"""scripts/kb-theme-cluster.py

Read JSONL of issue metadata; cluster by the literal "【...】" prefixes in summary.
No AI inference, no fuzzy matching — strictly bracket-based grouping.

Usage:
    python scripts/kb-theme-cluster.py --in <meta.jsonl> --out <themes.md>
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

# Match all 【...】 blocks, capture inner text
BRACKET_RE = re.compile(r"【([^】]+)】")


def primary_tag(summary: str) -> str | None:
    """Return the first 【...】 inner text, or None if no bracket."""
    m = BRACKET_RE.search(summary or "")
    return m.group(1).strip() if m else None


def first_segment(tag: str) -> str:
    """Split tag by '-' or '－' or '——' or space; return first segment as coarse cluster."""
    parts = re.split(r"[-－—\s—]+", tag)
    return parts[0].strip() if parts else tag


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=40, help="Top N themes to show in full")
    args = ap.parse_args()

    issues = []
    with Path(args.inp).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            issues.append(json.loads(line))

    total = len(issues)
    no_bracket = []
    primary_counts: Counter[str] = Counter()
    coarse_counts: Counter[str] = Counter()
    coarse_to_keys: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    primary_to_keys: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    issuetype_counts: Counter[str] = Counter()
    year_counts: Counter[str] = Counter()

    for issue in issues:
        key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary", "") or ""
        issuetype = ((fields.get("issuetype") or {}).get("name")) or "?"
        created = (fields.get("created") or "")[:4]
        issuetype_counts[issuetype] += 1
        year_counts[created] += 1
        p = primary_tag(summary)
        if not p:
            no_bracket.append((key, summary))
            continue
        primary_counts[p] += 1
        primary_to_keys[p].append((key, summary))
        c = first_segment(p)
        coarse_counts[c] += 1
        coarse_to_keys[c].append((key, summary))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write(f"# 乐檬WMS 工单主题地图\n\n")
        f.write(f"> 由 `scripts/kb-theme-cluster.py` 生成。\n")
        f.write(f"> 数据来源：`_kb/projects/wms/_bulk-index/issues-meta.jsonl`（`{args.inp}`）。\n")
        f.write(f"> **严格按 summary 中的 `【...】` 字面前缀分组**，无 AI 推断、无模糊匹配。\n\n")

        f.write(f"## 总览\n\n")
        f.write(f"| 项 | 数 |\n|---|---|\n")
        f.write(f"| 总工单数 | {total} |\n")
        f.write(f"| 含 `【...】` 前缀 | {total - len(no_bracket)} ({(total - len(no_bracket))*100//total}%) |\n")
        f.write(f"| 不含前缀（裸 summary） | {len(no_bracket)} |\n")
        f.write(f"| 唯一一级标签 `【X】` 数 | {len(primary_counts)} |\n")
        f.write(f"| 唯一粗粒度（标签首段）数 | {len(coarse_counts)} |\n")

        f.write(f"\n### issuetype 分布\n\n")
        f.write(f"| issuetype | 数 |\n|---|---|\n")
        for t, n in issuetype_counts.most_common():
            f.write(f"| {t} | {n} |\n")

        f.write(f"\n### 创建年份分布\n\n")
        f.write(f"| 年 | 数 |\n|---|---|\n")
        for y, n in sorted(year_counts.items()):
            f.write(f"| {y} | {n} |\n")

        f.write(f"\n---\n\n## 一级标签 Top {args.top}（按工单数倒序）\n\n")
        f.write(f"> 这是 summary 中 `【...】` 字面内容（如「仓配App-拣货」「标准参数」），不做合并。\n")
        f.write(f"> 用于决定**主题深读优先级**。后面的 keys 是该主题下最近 5 条样本，供你浏览代表性。\n\n")
        for tag, n in primary_counts.most_common(args.top):
            f.write(f"### 【{tag}】 — {n} 条\n\n")
            samples = primary_to_keys[tag][:5]
            for key, summary in samples:
                summary_short = summary[:80] + ("…" if len(summary) > 80 else "")
                f.write(f"- `{key}` — {summary_short}\n")
            if n > 5:
                f.write(f"- … 另有 {n-5} 条\n")
            f.write("\n")

        f.write(f"\n---\n\n## 粗粒度聚类（按一级标签首段合并）Top {args.top}\n\n")
        f.write(f"> 标签首段相同的合并到一起（如「仓配App-拣货」「仓配App-收货」都归到「仓配App」）。\n")
        f.write(f"> 这一栏用于发现**大模块**热度。\n\n")
        for coarse, n in coarse_counts.most_common(args.top):
            f.write(f"### {coarse} — {n} 条\n\n")
            f.write(f"- 一级标签细分：")
            inner = Counter()
            for key, summary in coarse_to_keys[coarse]:
                p = primary_tag(summary)
                if p:
                    inner[p] += 1
            top_inner = inner.most_common(8)
            f.write("、".join(f"`【{t}】×{c}`" for t, c in top_inner))
            if len(inner) > 8:
                f.write(f"…（共 {len(inner)} 个不同一级标签）")
            f.write("\n\n")

        f.write(f"\n---\n\n## 完整一级标签清单（全部 {len(primary_counts)} 个，按数倒序）\n\n")
        f.write("| 标签 | 数 |\n|---|---|\n")
        for tag, n in primary_counts.most_common():
            f.write(f"| `【{tag}】` | {n} |\n")

        f.write(f"\n---\n\n## 不含 `【...】` 前缀的工单（共 {len(no_bracket)}）\n\n")
        f.write("前 30 条样本（供你判断是否需要补充聚类规则）：\n\n")
        for key, summary in no_bracket[:30]:
            summary_short = summary[:100] + ("…" if len(summary) > 100 else "")
            f.write(f"- `{key}` — {summary_short}\n")
        if len(no_bracket) > 30:
            f.write(f"- … 另有 {len(no_bracket)-30} 条\n")

    print(f"DONE -> {out}")
    print(f"  total issues   : {total}")
    print(f"  primary tags   : {len(primary_counts)}")
    print(f"  coarse clusters: {len(coarse_counts)}")
    print(f"  no-bracket     : {len(no_bracket)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
