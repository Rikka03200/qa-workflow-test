#!/usr/bin/env python
"""scripts/kb-marathon.py

无人值守批量拉取脚本。按粗粒度主题优先级，对每个主题的全部工单 GET 全量内容
（description + 评论 + 附件元数据 + issuelinks），并追踪"见关联问题"链。

设计：
- 只调 GET (REST /rest/api/2/issue/<key>?fields=*all)，不调任何写接口
- 支持中断恢复：每条工单写完更新 progress.json，重启自动跳过已完成
- 每个主题写一个 NDJSON 文件 _kb/projects/wms/_bulk-index/full-marathon/<theme>.jsonl
- "见关联问题"自动追溯：如果 description 包含此关键词，递归拉所有 issuelinks（深度 ≤ 2）
- 黑名单主题：明显不是业务规则的（ELK 异常报警、打印格式、硬件对接、重构）

约束：READ-ONLY，全程不写 Jira。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from _load_env import parse_config  # noqa: E402

META_FILE = REPO / "_kb/projects/wms/_bulk-index/issues-meta.jsonl"
OUT_DIR = REPO / "_kb/projects/wms/_bulk-index/full-marathon"
PROGRESS_FILE = OUT_DIR / "progress.json"
LOG_FILE = OUT_DIR / "marathon.log"

BLACKLIST_COARSE = {
    "ELK异常报警", "新打印格式", "打印格式设置", "重构",
    "零食有鸣硬件对接", "硬件对接", "埋点", "日志",
}

LINKED_KEYWORDS = ["见关联问题", "见关联工单", "见关联", "关联问题"]
BRACKET_RE = re.compile(r"【([^】]+)】")


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def jira_get(path: str, token: str, base_url: str, retries: int = 3) -> dict:
    url = base_url.rstrip("/") + path
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # exponential backoff
    raise RuntimeError(f"GET {path} failed after {retries}: {last_err}")


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_progress(p: dict) -> None:
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS_FILE)


def coarse(summary: str) -> str | None:
    m = BRACKET_RE.search(summary or "")
    if not m:
        return None
    tag = m.group(1).strip()
    parts = re.split(r"[-－—\s—]+", tag)
    return parts[0].strip() if parts else tag


def load_meta() -> list[dict]:
    issues = []
    with META_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            issues.append(json.loads(line))
    return issues


def group_by_coarse(issues: list[dict]) -> dict[str, list[dict]]:
    g = defaultdict(list)
    for issue in issues:
        s = (issue.get("fields", {}).get("summary") or "")
        c = coarse(s)
        if not c:
            c = "_NO_BRACKET_"
        g[c].append(issue)
    return g


def fetch_full(key: str, token: str, base_url: str) -> dict:
    q = urllib.parse.urlencode({"fields": "*all", "expand": "comment,renderedFields"})
    return jira_get(f"/rest/api/2/issue/{key}?{q}", token, base_url)


def extract_link_keys(issue: dict) -> list[str]:
    """Return issuelinks' related keys (both inward/outward)."""
    out = []
    for link in (issue.get("fields", {}).get("issuelinks") or []):
        for side in ("inwardIssue", "outwardIssue"):
            inner = link.get(side)
            if inner and inner.get("key"):
                out.append(inner["key"])
    return out


def description_says_see_linked(issue: dict) -> bool:
    desc = (issue.get("fields", {}).get("description") or "")
    return any(kw in desc for kw in LINKED_KEYWORDS)


def process_theme(theme: str, issues: list[dict], token: str, base_url: str,
                  progress: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{theme}.jsonl"
    linked_dir = OUT_DIR / "_linked"
    linked_dir.mkdir(parents=True, exist_ok=True)

    theme_done = set(progress.get(theme, {}).get("done", []))
    linked_done = set(progress.setdefault("_linked", {}).get("done", []))

    log(f"Theme {theme!r}: {len(issues)} issues, {len(theme_done)} already done")

    if out_path.exists():
        mode = "a"
    else:
        mode = "w"

    with out_path.open(mode, encoding="utf-8") as f:
        for i, issue in enumerate(issues, 1):
            key = issue["key"]
            if key in theme_done:
                continue
            try:
                full = fetch_full(key, token, base_url)
            except Exception as e:
                log(f"  ERROR fetching {key}: {e}")
                time.sleep(2)
                continue
            f.write(json.dumps(full, ensure_ascii=False) + "\n")
            f.flush()
            theme_done.add(key)

            # 见关联问题 → 拉关联工单
            if description_says_see_linked(full):
                for lk in extract_link_keys(full):
                    if lk in linked_done:
                        continue
                    try:
                        linked = fetch_full(lk, token, base_url)
                        (linked_dir / f"{lk}.json").write_text(
                            json.dumps(linked, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        linked_done.add(lk)
                    except Exception as e:
                        log(f"  ERROR linked {lk}: {e}")
                    time.sleep(0.1)

            if i % 10 == 0:
                progress[theme] = {"done": sorted(theme_done), "total": len(issues)}
                progress["_linked"]["done"] = sorted(linked_done)
                save_progress(progress)
                log(f"  {theme}: {i}/{len(issues)} done")
            time.sleep(0.15)

    progress[theme] = {"done": sorted(theme_done), "total": len(issues), "completed_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    progress["_linked"]["done"] = sorted(linked_done)
    save_progress(progress)
    log(f"Theme {theme!r}: DONE ({len(theme_done)}/{len(issues)})")


def main() -> int:
    env = parse_config()
    base_url = env.get("JIRA_URL")
    token = env.get("JIRA_PERSONAL_TOKEN")
    if not (base_url and token):
        log("ERROR: JIRA_URL/PAT not configured")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log("=" * 70)
    log(f"MARATHON START (read-only)")
    log(f"meta file: {META_FILE}")
    log(f"out dir  : {OUT_DIR}")

    issues = load_meta()
    log(f"loaded {len(issues)} meta records")

    groups = group_by_coarse(issues)
    # 按工单数倒序排，并应用黑名单
    ordered_themes = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    ordered_themes = [(t, lst) for t, lst in ordered_themes if t not in BLACKLIST_COARSE]
    # 把 _NO_BRACKET_（无标签工单，多为客户名命名）放最后——价值低，最后再跑
    ordered_themes.sort(key=lambda kv: (1 if kv[0] == "_NO_BRACKET_" else 0, -len(kv[1])))
    log(f"themes (post-blacklist) : {len(ordered_themes)}")
    log(f"top 15 themes:")
    for t, lst in ordered_themes[:15]:
        log(f"  {t}: {len(lst)}")

    progress = load_progress()

    for theme, lst in ordered_themes:
        try:
            process_theme(theme, lst, token, base_url, progress)
        except KeyboardInterrupt:
            log("interrupted")
            return 130
        except Exception as e:
            log(f"theme {theme} crashed: {e}; continuing next")
            time.sleep(5)

    log("MARATHON COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
