#!/usr/bin/env python
"""
scripts/mcp-atlassian-wrapper.py

Claude Code / Codex 启动 atlassian MCP 时实际执行的入口。
做三件事：
  1. 读 config/config.local.yaml（凭证只在本仓库的 gitignore 文件里）
  2. 把凭证 + READ_ONLY_MODE 注入到**子进程**的环境变量（不写 Windows 注册表）
  3. exec `uvx mcp-atlassian`，让 MCP server 拿到正确 env
"""

from __future__ import annotations

import os
import sys
import subprocess

_BASE_ENV_ALLOWLIST = {
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "UV_CACHE_DIR",
    "UV_PYTHON",
    "UV_PYTHON_INSTALL_DIR",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}

# 复用同目录的 _load_env.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load_env import parse_config, validate  # noqa: E402


def subprocess_env(env_overrides: dict[str, str], base: dict[str, str] | None = None) -> dict[str, str]:
    """Build a narrow env for mcp-atlassian; do not leak unrelated secrets."""
    source = base if base is not None else os.environ
    env = {key: value for key, value in source.items() if key in _BASE_ENV_ALLOWLIST and value}
    env.update(env_overrides)
    env["DISABLE_JIRA_MARKUP_TRANSLATION"] = "true"
    return env


def main() -> int:
    env_overrides = parse_config()
    validate(env_overrides)

    env = subprocess_env(env_overrides)

    try:
        # 注：env 仅传给子进程，**不修改** os.environ 之外的任何持久存储
        return subprocess.call(["uvx", "mcp-atlassian"], env=env)
    except FileNotFoundError:
        sys.stderr.write(
            "[mcp-atlassian-wrapper] 未找到 uvx。请安装：\n"
            "  - Windows: winget install astral-sh.uv\n"
            "  - 或 pip install uv\n"
            "  - 或在 .mcp.json 切换到 docker 模式（见 scripts/README.md）\n"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
