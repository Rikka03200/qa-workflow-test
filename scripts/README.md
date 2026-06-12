# scripts/ 启动脚本

读 `config/config.local.yaml`，把凭证导出为环境变量，供 `.mcp.json` 中的 MCP server 读取。

## 文件

| 文件 | 平台 | 用法 |
|---|---|---|
| `load-config.ps1` | Windows PowerShell | `.\scripts\load-config.ps1` |
| `load-config.sh` | bash / Git Bash / WSL | `source ./scripts/load-config.sh` |
| `select_sprint.py` | Python | `python scripts/select_sprint.py --product wms --sprint 2026-06-09`（**只读选单**：JQL + 拆单/主单去重 + 覆盖判定 → 报告，不生成产物） |
| `run_sprint.py --select` | Python | `python scripts/run_sprint.py --product wms --sprint 2026-06-09 --select [--dry-run]`（选单 + Jira 规则检索证据 + 弱模型批量驱动 + 断点续跑 + 看板 + 记账） |
| `jira_fetch.py` | Python | `python scripts/jira_fetch.py EAR-240883 [--with-links]`（只读 Jira；含 `search()`/`resolve_sprint_ids()` 库函数） |
| `check-ticket-artifacts.py` | Python | `python scripts/check-ticket-artifacts.py tickets/wms/2026-06-02` |
| `validate-questions.py` | Python | `python scripts/validate-questions.py tickets/wms/2026-06-02/<ticket>/questions.md`（稳定校验 `questions.md` 人工答题入口格式） |
| `normalize-questions.py` | Python | `python scripts/normalize-questions.py tickets/wms/2026-06-02/<ticket>/questions.md`（自动归一可确定的 `questions.md` 结构问题，不改业务答案） |
| `validate-test-design.py` | Python | `python scripts/validate-test-design.py tickets/wms/2026-06-02/<ticket>/test-design.json` |

## Sprint 批量选单（select_sprint）

选单规则（确定性，固化于脚本 + `CLAUDE.md §4.5`，不必每次口头交代）：

1. 候选 = JQL(`project ∈ 配置 ∧ resolution=Unresolved ∧ cf[10020]=测试员`) ∩ sprint(指定日期)，再客户端滤 `issuetype=提高`。
2. 拆单（标题 `<主单标题>--平台后缀`）按**标题精确匹配关联主单**（去掉后缀后正好等于某关联工单标题）归到同一"功能特性"，每特性只产一份产物；匹配不到主单则当独立单跑并在报告标"需人工复核"。
3. 决策：特性已走过(账本/其他 sprint 已有通过校验产物)→跳过；主单在本 sprint→跑主单跳拆单；主单缺席→跑一个拆单（读主单需求）。
4. 跑完成功记账 `coverage-ledger.json`；将来主单所在 sprint 自动跳过。

```bash
# 只读选单 + 报告（零额度）
python scripts/select_sprint.py --product wms --sprint 2026-06-09

# 选单 + 批量生成（弱模型，不耗主 Claude；会先生成 _jira-search.md 作为 Jira 规则检索证据）
python scripts/run_sprint.py --product wms --sprint 2026-06-09 --select
python scripts/run_sprint.py --product wms --sprint 2026-06-09 --select --dry-run  # 只选不跑

# 分段编排（推荐：证据消解→人工只答无据项）
python scripts/run_sprint.py --product wms --sprint 2026-06-09 --select --until questions   # 只生成到 questions 停
#   → /qa:resolve 自动消解有据待确认 → 人工答剩余 → 再续跑：
python scripts/run_sprint.py --product wms --sprint 2026-06-09 --select --resume-after-questions
#   （续跑必带 --select：限定范围为 run_list 并登记覆盖账本）

# 报告/看板/账本都在：
#   tickets/<product>/.sprint-state/_selection-<date>.md      选单报告（run/skip 决策表）
#   tickets/<product>/.sprint-state/_sprint-summary-<date>.md 生成看板
#   tickets/<product>/.sprint-state/coverage-ledger.json      跨 sprint 覆盖账本
```

> 看板 id：select_sprint 把 sprint 日期解析成 sprint id 需要它。默认从 `config.products.<product>.jira_board_id`
> 或本地 `_jira-raw.json` 的 `rapidViewId` 推导（WMS=236）；都拿不到时报错，可加 `--board 236`。

**注意**：必须用 `source`（bash）或直接调用（PowerShell），而不是 `./load-config.sh`——否则 env 只在子进程生效，主 shell 拿不到。

## 前置依赖

MCP server 由 `uvx` 运行，需要本机已安装 `uv`：

```bash
# Windows
winget install astral-sh.uv
# 或 pip install uv

# 验证
uvx --version
```

如果你不想装 `uv`，可改 `.mcp.json` 用 Docker：

```json
"command": "docker",
"args": ["run", "--rm", "-i", "ghcr.io/sooperset/mcp-atlassian:latest"]
```

## 工单产物校验

```bash
# 检查一整个 Sprint 的 Markdown 产物齐套、章节规范和 JSON 基础契约
python scripts/check-ticket-artifacts.py tickets/wms/2026-06-02

# 只看按工单聚合的摘要，适合批量巡检
python scripts/check-ticket-artifacts.py --summary-only tickets/wms/2026-06-02

# 自动归一 + 稳定检查 questions.md 人工答题入口格式
python scripts/normalize-questions.py tickets/wms/2026-06-02/EAR-249774/questions.md
python scripts/validate-questions.py tickets/wms/2026-06-02/EAR-249774/questions.md

# 深度检查 CodeArts JSON 契约
python scripts/validate-test-design.py tickets/wms/2026-06-02/EAR-249774/test-design.json
```

## 校验流程

```powershell
# 1. 复制并填配置
cp config/config.example.yaml config/config.local.yaml
notepad config/config.local.yaml  # 填 URL + token

# 2. 加载到 env
.\scripts\load-config.ps1
# 应看到：✓ JIRA_URL = ...   ✓ JIRA_PERSONAL_TOKEN = (len=xxx)

# 3. 测试 MCP（启动 Claude Code）
claude
# 进对话后输入 /mcp 查看 atlassian server 是否 connected
```

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `未找到 config.local.yaml` | 没复制模板 | `cp config/config.example.yaml config/config.local.yaml` |
| MCP server `not connected` | `uvx` 没装 / 凭证错 | `uvx --version` 验证；重跑 load-config |
| 401 Unauthorized | token 无效 / 过期 | Jira → 头像 → Personal Access Tokens → 重新生成 |
| SSL 错误 | 自签证书 | 配置里 `ssl_verify: false`（仅自建实例临时用）|
