#!/usr/bin/env python
"""
scripts/jira_fetch.py

只读 Jira REST 拉取——不依赖 MCP，复用 _load_env 的凭证（与 kb-marathon.py 同款直连）。
仅做 HTTP GET；绝不写 Jira；不打印 token 明文。

用法：
  python scripts/jira_fetch.py EAR-240883                      打印摘要
  python scripts/jira_fetch.py EAR-240883 --raw out.json       另存原始 JSON
  python scripts/jira_fetch.py EAR-240883 --with-links         同时拉关联工单摘要
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from _load_env import parse_config  # noqa: E402
try:
    import jira_cache  # noqa: E402
except Exception:  # noqa: BLE001
    jira_cache = None

# 与 CLAUDE.md §5 约定一致的字段集（含 comment 与自定义字段）
FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,created,updated,labels,"
    "components,fixVersions,description,attachment,issuelinks,duedate,comment,"
    "customfield_10020,customfield_10125"
)

_RATE_TOKENS = 0.0
_RATE_LAST = 0.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _rate_limit() -> None:
    """Process-local token bucket for Jira REST calls."""
    global _RATE_LAST, _RATE_TOKENS
    qps = _env_float("QA_JIRA_RATE_QPS", 1.0)
    burst = max(1.0, _env_float("QA_JIRA_RATE_BURST", 5.0))
    if qps <= 0:
        return
    now = time.monotonic()
    if _RATE_LAST <= 0:
        _RATE_LAST = now
        _RATE_TOKENS = burst
    elapsed = max(0.0, now - _RATE_LAST)
    _RATE_LAST = now
    _RATE_TOKENS = min(burst, _RATE_TOKENS + elapsed * qps)
    if _RATE_TOKENS >= 1.0:
        _RATE_TOKENS -= 1.0
        return
    wait = (1.0 - _RATE_TOKENS) / qps
    time.sleep(wait)
    _RATE_LAST = time.monotonic()
    _RATE_TOKENS = 0.0


def _auth_header(env: dict[str, str]) -> str:
    pat = env.get("JIRA_PERSONAL_TOKEN")
    if pat:
        return "Bearer " + pat
    user, tok = env.get("JIRA_USERNAME"), env.get("JIRA_API_TOKEN")
    if user and tok:
        return "Basic " + base64.b64encode(f"{user}:{tok}".encode()).decode()
    raise SystemExit("[jira_fetch] 无 Jira 凭证（JIRA_PERSONAL_TOKEN 或 JIRA_USERNAME+JIRA_API_TOKEN）")


def _ssl_ctx(env: dict[str, str]):
    if str(env.get("JIRA_SSL_VERIFY", "true")).lower() == "false":
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c
    return None


def get_issue(key: str, env: dict[str, str] | None = None,
              fields: str = FIELDS, expand: str = "comment") -> dict:
    env = env or parse_config()
    if jira_cache and fields == FIELDS and expand == "comment":
        cached = jira_cache.load_issue(key, env)
        if cached is not None:
            return cached
    base = env.get("JIRA_URL")
    if not base:
        raise SystemExit("[jira_fetch] JIRA_URL 未配置")
    from urllib.parse import quote
    # 仅对 path 段的 key 做百分号编码（防畸形 key 如含空格破坏请求行）；fields/expand 为内部常量
    url = f"{base}/rest/api/2/issue/{quote(key, safe='')}?fields={fields}&expand={expand}"
    req = urllib.request.Request(
        url, headers={"Authorization": _auth_header(env), "Accept": "application/json"}
    )
    _rate_limit()
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx(env)) as r:
            issue = json.loads(r.read().decode("utf-8"))
            if jira_cache and fields == FIELDS and expand == "comment":
                jira_cache.save_issue(issue, env=env)
            return issue
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise SystemExit(f"[jira_fetch] HTTP {e.code} {e.reason} — {key}\n{body}")
    except Exception as e:
        raise SystemExit(f"[jira_fetch] 请求失败 {type(e).__name__}: {e}\n（检查网络/VPN 能否访问 {base}）")


def _request(url: str, env: dict[str, str], data: bytes | None = None,
             method: str | None = None) -> dict:
    """通用 JSON 请求（GET / POST），复用鉴权与 SSL；HTTP 错误带响应体便于排查。"""
    headers = {"Authorization": _auth_header(env), "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    _rate_limit()
    try:
        with urllib.request.urlopen(req, timeout=40, context=_ssl_ctx(env)) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:400]
        except Exception:
            pass
        raise SystemExit(f"[jira_fetch] HTTP {e.code} {e.reason} — {url.split('?')[0]}\n{body}")
    except Exception as e:
        raise SystemExit(f"[jira_fetch] 请求失败 {type(e).__name__}: {e}\n（检查网络/VPN 能否访问 {url.split('/rest')[0]}）")


def search(jql: str, fields, env: dict[str, str] | None = None,
           max_results: int | None = None, page_size: int = 100) -> list[dict]:
    """JQL 搜索，自动翻页取全。

    **必须用 POST**：本实例的网关（Aliyun）会对 GET query 里的中文（如字段名「问题测试员」）
    回 405 拦截页；POST 把 jql/fields 放进 JSON body，URL 纯 ASCII，规避 WAF。
    JQL 也应保持 ASCII：实测 `issuetype = "提高"` 会被 Jira 以 400「域中没有"提高"值」拒绝，
    issuetype/问题测试员 等中文条件请放到调用方做客户端过滤，不要写进 JQL。
    """
    env = env or parse_config()
    base = env.get("JIRA_URL")
    if not base:
        raise SystemExit("[jira_fetch] JIRA_URL 未配置")
    url = f"{base}/rest/api/2/search"
    flist = fields.split(",") if isinstance(fields, str) else list(fields)
    issues: list[dict] = []
    start = 0
    while True:
        payload = json.dumps(
            {"jql": jql, "fields": flist, "startAt": start, "maxResults": page_size}
        ).encode("utf-8")
        data = _request(url, env, data=payload, method="POST")
        batch = data.get("issues", []) or []
        issues.extend(batch)
        total = int(data.get("total", 0) or 0)
        start += len(batch)
        if max_results and len(issues) >= max_results:
            return issues[:max_results]
        if not batch or start >= total:           # 空批兜底，防 total 异常时死循环
            break
    return issues


def myself(env: dict[str, str] | None = None) -> str:
    """返回当前 Jira 账号的显示名（用于把『问题测试员=当前用户』落到 JQL）。"""
    env = env or parse_config()
    base = env.get("JIRA_URL")
    if not base:
        raise SystemExit("[jira_fetch] JIRA_URL 未配置")
    data = _request(f"{base}/rest/api/2/myself", env)
    return data.get("displayName") or data.get("name") or ""


def list_sprints(board_id, env: dict[str, str] | None = None) -> list[dict]:
    """列出某敏捷看板（rapidView）的全部 sprint（含历史/未来）：[{id,name,state,...}]。
    用 greenhopper sprintquery（实测一次返回全量，比 /agile/1.0 分页稳）。"""
    env = env or parse_config()
    base = env.get("JIRA_URL")
    if not base:
        raise SystemExit("[jira_fetch] JIRA_URL 未配置")
    url = (f"{base}/rest/greenhopper/1.0/sprintquery/{board_id}"
           "?includeFutureSprints=true&includeHistoricSprints=true")
    data = _request(url, env)
    return data.get("sprints") or data.get("values") or []


def resolve_sprint_ids(date: str, board_id, env: dict[str, str] | None = None
                       ) -> tuple[list[int], list[dict]]:
    """把 sprint 日期（如 2026-06-02）解析成 sprint id 列表。
    匹配 name 以该日期开头的 sprint（兼容 `2026-06-02.BETA` / `2026-05-12` / `…不发版`）。
    返回 (ids, matched_meta)；matched_meta 供报告展示 name/state。"""
    sprints = list_sprints(board_id, env)
    # 日期可能在 name 任意位置（如 `预排期2026-06-16.BETA`/`Mars-2026-06-16.1`/`2026-06-16.BETA`），
    # 用完整 10 位日期做子串匹配（已锚定，不会被 `2026-06-1` 之类误命中）。
    matched = [s for s in sprints if date in str(s.get("name", ""))]
    # 同日期可能有多个（如正式 + .BETA）；全取（JQL `sprint in (...)`）。
    ids = [int(s["id"]) for s in matched if s.get("id") is not None]
    return ids, matched


def summarize(issue: dict) -> str:
    f = issue.get("fields", {}) or {}
    L: list[str] = []
    L.append(f"# {issue.get('key')}: {f.get('summary', '')}")
    it = (f.get("issuetype") or {}).get("name")
    st = (f.get("status") or {}).get("name")
    pr = (f.get("priority") or {}).get("name")
    L.append(f"- 类型: {it}  状态: {st}  优先级: {pr}")
    comps = ",".join(c.get("name", "") for c in (f.get("components") or []))
    L.append(f"- 组件: {comps}  标签: {','.join(f.get('labels') or [])}")
    L.append(f"- Sprint(customfield_10125): {f.get('customfield_10125')}")
    L.append(f"- 经办人: {(f.get('assignee') or {}).get('displayName')}  "
             f"报告人: {(f.get('reporter') or {}).get('displayName')}")
    L.append("\n## 描述（raw wiki markup）\n")
    L.append(f.get("description") or "(空)")
    comments = ((f.get("comment") or {}).get("comments")) or []
    L.append(f"\n## 评论（{len(comments)} 条）\n")
    for c in comments:
        au = (c.get("author") or {}).get("displayName")
        L.append(f"--- {au} @ {c.get('created')}\n{c.get('body')}\n")
    links = f.get("issuelinks") or []
    L.append(f"\n## 关联工单（{len(links)}）\n")
    for l in links:
        t = l.get("type") or {}
        out, inw = l.get("outwardIssue"), l.get("inwardIssue")
        oi = out or inw
        rel = t.get("outward") if out else t.get("inward")
        if oi:
            of = oi.get("fields") or {}
            L.append(f"- [{rel}] {oi.get('key')}: {of.get('summary')} "
                     f"(status={(of.get('status') or {}).get('name')}, "
                     f"resolution={(of.get('resolution') or {}).get('name')})")
    atts = f.get("attachment") or []
    if atts:
        L.append(f"\n## 附件（{len(atts)}）\n")
        for a in atts:
            L.append(f"- {a.get('filename')} ({a.get('size')}B, {a.get('mimeType')})")
    return "\n".join(str(x) for x in L)


def main() -> int:
    ap = argparse.ArgumentParser(description="只读 Jira REST 拉取")
    ap.add_argument("key")
    ap.add_argument("--raw", help="另存原始 JSON 到该路径")
    ap.add_argument("--with-links", action="store_true", help="同时拉关联工单摘要")
    args = ap.parse_args()
    env = parse_config()
    issue = get_issue(args.key, env)
    if args.raw:
        Path(args.raw).write_text(json.dumps(issue, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[saved] {args.raw}\n")
    print(summarize(issue))
    if args.with_links:
        for l in (issue.get("fields") or {}).get("issuelinks") or []:
            oi = l.get("outwardIssue") or l.get("inwardIssue")
            if not oi:
                continue
            print(f"\n\n########## 关联工单 {oi.get('key')} ##########")
            try:
                print(summarize(get_issue(oi.get("key"), env)))
            except SystemExit as e:
                print(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
