#!/usr/bin/env python
"""scripts/kb-marathon-deep.py

深度追溯：读已下载的 _linked/*.json，对其中每条工单的 issuelinks 递归拉取，
形成"原始命中 → 直接关联 → 二跳关联"的全景图。

只读 Jira。支持中断恢复。
"""
from __future__ import annotations
import json, sys, time, urllib.parse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from _load_env import parse_config  # noqa: E402

LINKED_DIR = REPO / "_kb/projects/wms/_bulk-index/full-marathon/_linked"
OUT_DIR = REPO / "_kb/projects/wms/_bulk-index/full-marathon/_linked-depth2"
PROGRESS = OUT_DIR / "progress.json"
LOG = OUT_DIR / "deep.log"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    sys.stderr.write(line + "\n"); sys.stderr.flush()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def jira_get(path, token, base_url, retries=3):
    url = base_url.rstrip("/") + path
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise


def load_progress():
    if PROGRESS.exists():
        try:
            return json.loads(PROGRESS.read_text(encoding="utf-8"))
        except Exception:
            return {"done": []}
    return {"done": []}


def save_progress(p):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS)


def main():
    env = parse_config()
    base_url, token = env.get("JIRA_URL"), env.get("JIRA_PERSONAL_TOKEN")
    if not (base_url and token):
        log("ERROR: no creds"); return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log(f"=" * 70)
    log("DEEP MARATHON START (read-only, depth=2 issuelinks)")

    # 收集所有 _linked 工单的 issuelinks 目标
    targets = set()
    already_have = set()
    # 已有的：原始 + 直接关联
    for f in LINKED_DIR.glob("*.json"):
        already_have.add(f.stem)
        try:
            issue = json.loads(f.read_text(encoding="utf-8"))
            for link in (issue.get("fields", {}).get("issuelinks") or []):
                for side in ("inwardIssue", "outwardIssue"):
                    inner = link.get(side)
                    if inner and inner.get("key"):
                        targets.add(inner["key"])
        except Exception:
            pass

    # 原始命中工单的 keys（issues-meta.jsonl）
    meta = REPO / "_kb/projects/wms/_bulk-index/issues-meta.jsonl"
    with meta.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                already_have.add(json.loads(line)["key"])

    todo = sorted(targets - already_have)
    log(f"_linked工单数: {len(list(LINKED_DIR.glob('*.json')))}")
    log(f"二跳关联候选: {len(targets)}")
    log(f"已有的（原始+直接关联）: {len(already_have)}")
    log(f"实际要拉: {len(todo)}")

    progress = load_progress()
    done = set(progress.get("done", []))

    for i, key in enumerate(todo, 1):
        if key in done:
            continue
        if (OUT_DIR / f"{key}.json").exists():
            done.add(key); continue
        try:
            q = urllib.parse.urlencode({"fields": "*all", "expand": "comment,renderedFields"})
            full = jira_get(f"/rest/api/2/issue/{key}?{q}", token, base_url)
            (OUT_DIR / f"{key}.json").write_text(
                json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
            done.add(key)
        except Exception as e:
            log(f"  ERROR {key}: {e}")
        if i % 20 == 0:
            save_progress({"done": sorted(done), "total": len(todo)})
            log(f"  {i}/{len(todo)} done")
        time.sleep(0.12)

    save_progress({"done": sorted(done), "total": len(todo), "completed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    log(f"DEEP MARATHON COMPLETE: {len(done)}/{len(todo)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
