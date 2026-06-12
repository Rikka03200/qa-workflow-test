#!/usr/bin/env python
"""
scripts/_load_env.py

共享的配置 → env 解析器。
读 config/config.local.yaml，按 KEY=VAL 行输出，shell 包装器再回填到 env。

也是 mcp-atlassian-wrapper.py 的工具函数源。

用法：
  python scripts/_load_env.py                    输出 KEY=VAL 行
  python scripts/_load_env.py --check            仅校验，不输出
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PLACEHOLDERS = {"REPLACE_ME", "REPLACE_ME_OR_LEAVE_BLANK", "", None}
# Hostnames that signal "not yet configured"—treat as unset to avoid the MCP
# trying to connect to fake/example endpoints.
EXAMPLE_HOST_SUFFIXES = (".example.com", ".example.org", ".company.com")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "config" / "config.local.yaml"


def load_raw_config() -> dict:
    """读 config.local.yaml 返回原始嵌套 dict（含 products/selection 等结构化段）。
    parse_config 派生扁平 env 用它；select_sprint 等需要嵌套配置的脚本也用它。"""
    if not CONFIG_FILE.exists():
        sys.stderr.write(
            f"[_load_env] 未找到 {CONFIG_FILE}\n"
            "请先：cp config/config.example.yaml config/config.local.yaml，再填真实凭证。\n"
        )
        sys.exit(1)
    try:
        import yaml
    except ImportError:
        sys.stderr.write("[_load_env] 缺少 pyyaml。运行：pip install pyyaml\n")
        sys.exit(1)
    try:
        return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        sys.stderr.write(f"[_load_env] yaml 解析失败：{exc}\n")
        sys.exit(1)



def parse_config() -> dict[str, str]:
    """Read yaml, return env dict to inject."""
    cfg = load_raw_config()

    out: dict[str, str] = {}

    def setenv(key: str, val, strip_slash: bool = False) -> None:
        # 仅接受标量；list/dict（常因 yaml 缩进写错）会让 `val in PLACEHOLDERS` 抛 TypeError，先挡掉并告警
        if val is not None and not isinstance(val, (str, int, float, bool)):
            sys.stderr.write(f"[_load_env] 跳过 {key}：值不是标量（{type(val).__name__}），请检查 config 缩进\n")
            return
        if val in PLACEHOLDERS:
            return
        s = str(val)
        if strip_slash:
            s = s.rstrip("/")
        # 跳过示例/占位主机名，避免 MCP 去连假端点。按主机名“真后缀”匹配（非子串），避免误伤合法内网域名。
        if key.endswith("_URL"):
            try:
                from urllib.parse import urlsplit
                host = urlsplit(s).hostname or s
            except Exception:
                host = s
            if any(host == sfx.lstrip(".") or host.endswith(sfx) for sfx in EXAMPLE_HOST_SUFFIXES):
                return
        out[key] = s

    j = cfg.get("jira") or {}
    setenv("JIRA_URL", j.get("url"), strip_slash=True)
    setenv("JIRA_PERSONAL_TOKEN", j.get("personal_access_token"))
    setenv("JIRA_USERNAME", j.get("username"))
    setenv("JIRA_API_TOKEN", j.get("api_token"))
    if j.get("ssl_verify") is not None:
        out["JIRA_SSL_VERIFY"] = "true" if j.get("ssl_verify") else "false"

    c = cfg.get("confluence") or {}
    setenv("CONFLUENCE_URL", c.get("url"), strip_slash=True)
    setenv("CONFLUENCE_PERSONAL_TOKEN", c.get("personal_access_token"))
    setenv("CONFLUENCE_USERNAME", c.get("username"))
    setenv("CONFLUENCE_API_TOKEN", c.get("api_token"))
    if c.get("ssl_verify") is not None:
        out["CONFLUENCE_SSL_VERIFY"] = "true" if c.get("ssl_verify") else "false"

    # 廉价批量子代理（多模型协作）。仅反映 config，不在此做行为门控；
    # 是否启用由 CHEAP_MODEL_ENABLED 表示，调用方（驱动 / cheap_model.py）自行判断。
    # 兼容两处位置：ai.cheap_provider（example 中的规范位置）或顶层 cheap_provider。
    a = (cfg.get("ai") or {}).get("cheap_provider") or cfg.get("cheap_provider") or {}
    setenv("CHEAP_MODEL_BASE_URL", a.get("base_url"), strip_slash=True)
    setenv("CHEAP_MODEL_API_KEY", a.get("api_key"))
    setenv("CHEAP_MODEL_NAME", a.get("model"))
    setenv("CHEAP_MODEL_SMALL_NAME", a.get("small_model"))
    setenv("CHEAP_MODEL_PROVIDER", a.get("provider"))  # anthropic(默认) | openai
    if a.get("max_tokens") is not None:
        out["CHEAP_MODEL_MAX_TOKENS"] = str(a.get("max_tokens"))
    if a.get("enabled") is not None:
        out["CHEAP_MODEL_ENABLED"] = "true" if a.get("enabled") else "false"

    w = cfg.get("workflow") or {}
    write_ok = bool(w.get("allow_write_jira") or w.get("allow_write_confluence"))
    out["READ_ONLY_MODE"] = "false" if write_ok else "true"

    # 进程级 env 覆盖（向后兼容）：webapp 以触发用户自己的 Jira PAT 注入子进程，
    # 让弱链生成在该用户身份下拉 Jira（用户要求「各用各的 PAT」）。
    # 终端正常运行不设这些变量 → out 不变，行为与改动前完全一致。
    for k in ("JIRA_URL", "JIRA_PERSONAL_TOKEN", "JIRA_USERNAME", "JIRA_API_TOKEN"):
        v = os.environ.get(k)
        if v and v not in PLACEHOLDERS:
            out[k] = v.rstrip("/") if k.endswith("_URL") else v

    return out


def validate(env: dict[str, str]) -> None:
    if not env.get("JIRA_URL"):
        sys.stderr.write("[_load_env] jira.url 未配置或仍为 REPLACE_ME\n")
        sys.exit(1)
    if not (env.get("JIRA_PERSONAL_TOKEN") or env.get("JIRA_API_TOKEN")):
        sys.stderr.write(
            "[_load_env] jira 鉴权未配置：personal_access_token 或 "
            "username+api_token 至少二选一\n"
        )
        sys.exit(1)


def main() -> int:
    env = parse_config()
    if "--check" in sys.argv:
        validate(env)
        return 0
    for k, v in env.items():
        # 输出 KEY=VAL，调用方按行解析；token 中的空格/特殊字符按原样输出
        print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
