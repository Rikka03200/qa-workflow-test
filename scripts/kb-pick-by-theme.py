#!/usr/bin/env python
"""scripts/kb-pick-by-theme.py

Pick top-N issue keys per theme from the metadata JSONL.

Theme matching is **strict literal**: summary must contain the exact "【<theme>...】" or
the coarse first-segment match (eg. "标准参数" matches "【标准参数】" but not "【标准参数-上架】"
unless --coarse is set).

Usage:
    python scripts/kb-pick-by-theme.py --in <meta.jsonl> --theme "标准参数" --top 15 --out <picks.jsonl>
    python scripts/kb-pick-by-theme.py --in <meta.jsonl> --theme "仓配App" --coarse --top 15 --out <picks.jsonl>
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

BRACKET_RE = re.compile(r"【([^】]+)】")


def primary_tag(summary: str) -> str | None:
    m = BRACKET_RE.search(summary or "")
    return m.group(1).strip() if m else None


def coarse_segment(tag: str) -> str:
    parts = re.split(r"[-－—\s—]+", tag)
    return parts[0].strip() if parts else tag


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--theme", required=True, help="exact primary tag, or coarse first-segment when --coarse")
    ap.add_argument("--coarse", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sort-by", choices=["updated", "created"], default="updated")
    args = ap.parse_args()

    matches = []
    with Path(args.inp).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            issue = json.loads(line)
            summary = (issue.get("fields", {}).get("summary") or "")
            tag = primary_tag(summary)
            if not tag:
                continue
            if args.coarse:
                if coarse_segment(tag) == args.theme:
                    matches.append(issue)
            else:
                if tag == args.theme:
                    matches.append(issue)

    matches.sort(key=lambda i: (i.get("fields", {}).get(args.sort_by) or ""), reverse=True)
    picks = matches[:args.top]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for issue in picks:
            f.write(json.dumps(issue, ensure_ascii=False) + "\n")

    print(f"theme={args.theme!r} coarse={args.coarse} matched={len(matches)} picked={len(picks)} -> {out}")
    for i in picks:
        s = i["fields"]["summary"]
        print(f"  {i['key']}  ({i['fields'].get(args.sort_by, '')[:10]})  {s[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
