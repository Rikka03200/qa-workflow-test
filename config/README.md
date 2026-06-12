# config/ 配置说明

凭证、产品线绑定、AI provider 全部在这里管理。**不要把 `config.local.yaml` 入 Git**——`.gitignore` 已配置。

## 快速开始

```bash
cp config/config.example.yaml config/config.local.yaml
# 编辑 config/config.local.yaml
```

## 字段说明

### `jira`

| 字段 | 必填 | 说明 |
|---|---|---|
| `url` | 是 | 公司 Jira 地址，不带末尾斜杠 |
| `personal_access_token` | 二选一 | Jira 8.14+ 的 PAT（推荐自建实例用）|
| `username` + `api_token` | 二选一 | Atlassian Cloud 或老版 Jira |
| `default_project_keys` | 是 | 你工单常出现的 project key 列表（如 `["EAR"]`）|
| `ssl_verify` | 否 | 自建实例自签证书可临时关，默认 `true` |

**怎么拿 PAT**：登录 Jira → 头像 → 个人设置 → Personal Access Tokens → 创建。

### `confluence`

字段同 Jira。`default_spaces` 填你常查规范的 space key（Confluence 页面 URL 里的 `/spaces/<KEY>/...`）。

### `ai`

- `anthropic.api_key`：如果用 Anthropic API 直连留这里；用 Claude Code 本地登录态可留空
- `openai.api_key`：Codex CLI 使用
- `cheap_provider`：多模型协作的廉价批量子代理（阿里百炼 Token Plan，Anthropic 兼容端点）——弱模型批量生成、主 Claude 终检。字段：`enabled`（填好 key 后改 `true`）、`base_url`（**非机密**，默认即 Token Plan 端点）、`api_key`（Token Plan 专属 key，**机密**，与百炼通用 key 不通用）、`model`（如 `qwen3.7-max`）、`max_tokens`、`small_model`（仅 `claude -p` 备选路径用）。连通自测：`python scripts/cheap_model.py --smoke`

### `products`

定义产品线 → Jira project + Confluence space 的绑定。**关键**——`/qa:new <product>` 用这里查产品是否合法。

新增产品线：复制一段，改 key 和绑定值，并到 `_kb/projects/` 建对应目录。

### `workflow`

- `current_sprint_date`：Jira 未返回 Sprint 时的手工兜底日期（`YYYY-MM-DD`）；正常留空，由 `/qa:new` 解析 Jira Sprint
- `default_tool`：`claude_code` 或 `codex`
- `allow_write_jira` / `allow_write_confluence`：默认 `false`——AI 只生成内容不直接写远端，保证你有人工审校窗口。

## 凭证如何流转

```
config.local.yaml  (gitignored, 仅你本地)
      │
      ▼
scripts/_load_env.py            ← 解析 yaml，输出 KEY=VAL
      │
      ├──→ scripts/mcp-atlassian-wrapper.py   ← 主路径
      │       │
      │       ▼
      │     uvx mcp-atlassian   (子进程，env 来自上面)
      │       │
      │       ▼
      │     Claude Code / Codex 调用 MCP 工具
      │
      └──→ scripts/load-config.{ps1,sh}   ← 可选工具，仅当你手工跑 curl/uvx 时
              │
              ▼
            当前 shell 进程的 env（关闭终端即清除）
```

**关键**：环境变量**只存活于子进程的生命周期**，Windows 注册表中的永久 env 不会被写入。
**绝不要**把凭证写进 `.mcp.json`、`CLAUDE.md`、`prompts/`、`tickets/`、`_kb/`——这些文件都入 Git。

## 校验配置

```bash
cd D:\Projects\qa-workflow
python scripts/_load_env.py            # 应输出 KEY=VAL 行（token 可见，谨慎贴出）
python scripts/_load_env.py --check    # 仅校验 Jira 必填，无输出
```

## 只读模式（默认）

`config.workflow.allow_write_jira` / `allow_write_confluence` 默认为 `false`。

启动时，`scripts/mcp-atlassian-wrapper.py` 会派生 `READ_ONLY_MODE=true`，
mcp-atlassian 服务端会拒绝所有写工具调用。即使 AI 试图调用 `jira_create_issue`，
也会在 `.claude/settings.json` 的 `deny` 列表里被 Claude Code 二次拦截。

要打开写权限：编辑 `config.local.yaml`，把开关改为 `true`，**重启 claude**。
