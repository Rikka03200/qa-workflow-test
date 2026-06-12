#!/usr/bin/env python
"""scripts/kb-bulk-extract.py

Bulk export Jira issue metadata for KB curation.

Usage:
    python scripts/kb-bulk-extract.py --jql "<JQL>" --out <path.jsonl>
    python scripts/kb-bulk-extract.py --jql "<JQL>" --out <path.jsonl> --full

--full mode also fetches description + comments per issue (slow; only use after metadata pass).

Reads credentials from config/config.local.yaml. PAT never logged.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from _load_env import parse_config  # noqa: E402


def jira_get(path: str, token: str, base_url: str) -> dict:
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_meta(jql: str, base_url: str, token: str, out_path: Path,
               page_size: int = 100, fields: str | None = None) -> int:
    fields = fields or "summary,status,issuetype,priority,components,labels,fixVersions,created,updated,assignee,reporter,customfield_10020,resolution"
    start = 0
    total_seen = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        while True:
            q = urllib.parse.urlencode({
                "jql": jql,
                "startAt": start,
                "maxResults": page_size,
                "fields": fields,
            })
            data = jira_get(f"/rest/api/2/search?{q}", token, base_url)
            total = data.get("total", 0)
            issues = data.get("issues") or []
            if not issues:
                break
            for issue in issues:
                f.write(json.dumps(issue, ensure_ascii=False) + "\n")
            total_seen += len(issues)
            print(f"  page startAt={start} got {len(issues)} (cum {total_seen}/{total})",
                  file=sys.stderr)
            start += len(issues)
            if start >= total:
                break
            time.sleep(0.1)  # polite
    return total_seen


def fetch_full(issue_key: str, base_url: str, token: str) -> dict:
    fields = "*all"
    q = urllib.parse.urlencode({"fields": fields, "expand": "comment"})
    return jira_get(f"/rest/api/2/issue/{issue_key}?{q}", token, base_url)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jql", required=True)
    ap.add_argument("--out", required=True, help="Output JSONL path")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--full", action="store_true",
                    help="Fetch description+comments for each issue (slow)")
    ap.add_argument("--from-meta", help="When --full, read keys from this JSONL")
    args = ap.parse_args()

    env = parse_config()
    base_url = env.get("JIRA_URL")
    token = env.get("JIRA_PERSONAL_TOKEN")
    if not base_url or not token:
        print("ERROR: JIRA_URL / JIRA_PERSONAL_TOKEN not configured", file=sys.stderr)
        return 1

    out = Path(args.out)
    if args.full:
        if not args.from_meta:
            print("ERROR: --full requires --from-meta <jsonl>", file=sys.stderr)
            return 1
        meta_path = Path(args.from_meta)
        keys = []
        with meta_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                d = json.loads(line)
                keys.append(d["key"])
        print(f"Fetching full content for {len(keys)} issues -> {out}", file=sys.stderr)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            for i, key in enumerate(keys, 1):
                try:
                    issue = fetch_full(key, base_url, token)
                    f.write(json.dumps(issue, ensure_ascii=False) + "\n")
                except Exception as e:
                    print(f"  ERROR {key}: {e}", file=sys.stderr)
                if i % 20 == 0:
                    print(f"  fetched {i}/{len(keys)}", file=sys.stderr)
                time.sleep(0.1)
        print(f"DONE: {len(keys)} issues -> {out}", file=sys.stderr)
        return 0

    print(f"JQL: {args.jql}", file=sys.stderr)
    print(f"OUT: {out}", file=sys.stderr)
    n = fetch_meta(args.jql, base_url, token, out, args.page_size)
    print(f"DONE: {n} issues -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
