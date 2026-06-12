# qa-workflow

AI 辅助的软件测试工作流仓库——把"需求理解、风险分析、用例生成、Bug 工单、验收备注、知识沉淀"做成可复用、跨产品线、跨工单的标准化流程。

## 是什么

一个**工程化的本地工作流项目**。你在 Claude Code（或 Codex）中打开本仓库，用预置的 slash 命令驱动 AI 完成每周的测试用例工作。AI 在固定的角色契约、抗幻觉规则、知识库约束下作业。

**核心理念**：AI 不掌握你们项目业务 → 我们用"项目知识库 + 规范模板 + 分步链路 + 人在回路"四件套，把"AI 凭印象写"变成"AI 基于真实文档写、能溯源、能复核"。

## 不是什么

- 不是一个 SaaS / 平台（v3 可能加前端）
- 不是自动化测试框架
- 不是 AI 工具的替代品（Claude Code/Codex 才是引擎，本仓库是规则与上下文）

## 快速开始

```bash
# 1. 克隆/打开本仓库
cd D:\Projects\qa-workflow

# 2. 复制配置模板，填入你的 Jira/Confluence/AI 凭证
cp config/config.example.yaml config/config.local.yaml
# 编辑 config/config.local.yaml（详见 config/README.md）
# config.local.yaml 已 gitignore；凭证只存在这里。

# 3. 在仓库根目录直接启动 Claude Code
cd D:\Projects\qa-workflow
claude
# .mcp.json 会自动调用 scripts/mcp-atlassian-wrapper.py，
# 它从 config.local.yaml 读凭证 → 以**进程级**环境变量启动 mcp-atlassian。
# Windows 永久环境变量不会被修改。

# 4. 在 Claude Code 中输入 /mcp 确认 atlassian 服务器 connected

# 5. 跑第一个工单（推荐：单工单编排器，一条命令串起全流程）
/qa:ticket wms EAR-246155 --run
#   依次：拉取 → 分析 → /qa:resolve 证据消解 → 停下让你只答仍需人工的少量问题
#         → 续跑生成 test-design.json → /qa:spot-check 强抽检

# 或手动分步（等价）：
/qa:new wms EAR-246155       # 建目录 + 拉 Jira
/qa:context                  # 业务上下文 + 关联工单
/qa:analyze                  # 风险 + 待确认问题
/qa:resolve EAR-246155       # 证据消解：自动答有据问题，只留真无据的给人工
#   ↑ 之后只回答 questions.md 中仍标「需人工」的问题
/qa:points                   # 测试点列表
/qa:skeleton                 # JSON 骨架（无 expect）
/qa:detail                   # 补全预期 → test-design.json
/qa:spot-check EAR-246155    # 强模型抽检
```

### 默认是**只读**模式

`config.workflow.allow_write_jira / allow_write_confluence` 默认 `false`：

- **服务端**：mcp-atlassian 收到 `READ_ONLY_MODE=true`，所有写工具直接拒绝
- **客户端**：`.claude/settings.json` 的 `deny` 列表明确拦截所有创建/修改/删除/上传/评论工具

要打开写权限，将两个开关改为 `true` 后**重启 claude**。建议保持只读直到你对工作流足够信任。

## 目录结构

```
qa-workflow/
├── CLAUDE.md                   AI 角色契约（Claude Code 读）
├── AGENTS.md                   AI 角色契约（Codex 读，内容同 CLAUDE.md）
├── README.md                   本文件
├── config/                     凭证与产品线配置
│   ├── config.example.yaml     模板（入仓）
│   ├── config.local.yaml       实际值（gitignore）
│   └── README.md               字段说明
├── scripts/                    启动脚本（读 config → 注入 env）
├── .mcp.json                   MCP server 注册（Atlassian Jira/Confluence）
├── .claude/                    Claude Code 专属
│   ├── settings.json           本地 Claude Code 设置
│   └── commands/qa/            slash 命令（→ /qa:xxx）
├── prompts/                    与 Codex 共用的纯 markdown prompt
├── _kb/                        知识库（长期演进）
│   ├── _global/                跨产品通用：用例规范、JSON 契约、QA 方法论
│   └── projects/<product>/     按产品线分支
│       └── wms/                WMS + 仓配 App
│           ├── modules.md
│           ├── rules.md
│           ├── terms.md
│           └── case-samples/
├── tickets/<product>/<sprint-date>/<EAR-xxxx>/   按 Jira Sprint 日期/工单组织
│   ├── README.md
│   ├── requirement.md          Jira 工单描述（拉取自动填）
│   ├── business-context.md     本工单业务上下文摘录
│   ├── linked-issues.md        关联工单 / 参考工单全量摘录
│   ├── analysis.md             需求分析 + 风险 + 待确认问题
│   ├── questions.md            待确认问题填写表单（用户填写，无问题时可不存在）
│   ├── test-points.md          测试点列表
│   ├── test-design.json        AI 最终产出
│   └── attachments/            空目录占位；Jira 附件只记录元数据，不下载
└── archive/                    旧版本归档
```

## slash 命令一览

| 命令 | 作用 |
|---|---|
| `/qa:sprint <product> <sprint-date>` | **批量选单**：按规则（测试员=林子宣 ∧ 提高 ∧ 未解决 + 拆单/主单去重 + 跨 sprint 覆盖）选出该 sprint 该走的工单，出选单报告；`--run` 直接驱动批量生成 |
| `/qa:new <product> <EAR-xxxx>` | 拉 Jira Sprint，按 `tickets/<product>/<sprint-date>/<EAR-xxxx>/` 建目录，并初始化 `requirement.md` |
| `/qa:context` | 检索 Confluence + `_kb/` 拼装 `business-context.md` |
| `/qa:analyze` | 需求理解、风险识别、待确认问题 → 输出 `analysis.md` + `questions.md` |
| `/qa:ticket <product> <EAR-xxxx>` | **单工单端到端编排**：分析 → 证据消解 → 人工答一次 → 生成 → 强抽检，一条命令串联 |
| `/qa:resolve <EAR-xxxx>` | 证据消解：人工回答前自动答掉有据待确认、补漏问，只把真无据的留给人工 |
| `/qa:points` | 测试点拆解（标题+优先级，不展开步骤）→ `test-points.md` |
| `/qa:skeleton` | 生成 JSON 骨架（到步骤标题，无 expect）→ `test-design.json` |
| `/qa:detail` | 给骨架补全 expect → 最终 `test-design.json` |
| `/qa:spot-check <EAR-xxxx>` | 强模型对抗式抽检（覆盖/算术/语义/越界/诚信）→ `_spot-check.md` |
| `/qa:kb-extract` | 评审后从工单产物提炼新业务规则建议入 `_kb/` |
| `/qa:kb-search <kw>` | 跨产品检索本地 + Confluence 知识 |

## 平台化基础设施（Web/Worker）

v3 Web 控制台已具备平台化地基：平台库 schema、用户/凭证/session 的 DB 优先真源、凭证加密、作业镜像、弱链子进程 env 白名单、产品配置抽象和 worker 队列骨架。钉钉与 Apifox 集成当前未启用。

### 平台数据库迁移

平台库与 `qa_knowledge` 知识库分离，优先读取 `QA_WEBAPP_DATABASE_URL`（其次 `QA_DATABASE_URL` / `DATABASE_URL`）：

```bash
pip install -r webapp/requirements-web.txt
export QA_WEBAPP_DATABASE_URL='postgresql+psycopg://qa_workflow:REPLACE_ME@localhost:5432/qa_workflow'
alembic -c deploy/alembic.ini upgrade head
```

`/healthz` 会返回 `platform_db`、`platform_reason`、`queue_depth`、`queue_depth_warn` 与 `queue_depth_threshold`。配置平台库后，`users`、`user_credentials`、`sessions` 成为身份与凭证真源；未配置平台库或平台库不可用时，Web 继续走 legacy 文件模式：`userdata/<user>/tickets`、`webapp/data/users.json` 与内存 `JobManager` 仍可用。平台库 SQLAlchemy engine 默认 `pool_size=5`、`max_overflow=5`、`pool_recycle=3600`；队列连接默认 `min_size=1`、`max_size=2`、`max_lifetime=3600`，可通过 `QA_DB_*` 与 `QA_QUEUE_*` 环境变量按进程数调整。Jira REST 默认启用进程内令牌桶（`QA_JIRA_RATE_QPS=1`、`QA_JIRA_RATE_BURST=5`）；平台库可用时，issue JSON 快照会缓存到 `tickets.metadata`，按 `QA_JIRA_CACHE_TTL_SECONDS=900` 复用。

### 凭证加密

用户 Jira PAT、弱模型 key、强模型 key 不再回显到页面。生产部署应配置 `QA_FERNET_KEYS`（逗号分隔，第一枚用于新加密，后续用于旧密文轮换解密）：

```bash
python - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
```

本地开发未设置 `QA_FERNET_KEYS` 时，Web 会在 gitignored 的 `webapp/data/fernet.key` 自动生成开发 key。不要把 Fernet key、数据库密码或 API key 写入仓库。

### Worker 骨架

Procrastinate worker 已可执行生成与强模型任务。`QA_USE_WORKER=1` 时，Web 只负责创建 `pipeline_runs` 并入队，worker 执行 `scripts/run_sprint.py` / 强模型复核并把状态与日志写回 `pipeline_runs`、`job_logs`；不设置该开关时保留 Web 线程执行作为本地回退。Docker 新环境默认启用 `QA_USE_DB_ARTIFACTS=1`：worker 会把 DB artifact 物化到 `.work/<run>/tickets` 执行，把白名单产物回灌到平台库，并导出当前用户 `userdata` 兼容缓存供旧页面/脚本读取；仅排障回退时设为 `0` 使用 P2 文件模式。

```bash
export QA_WEBAPP_DATABASE_URL='postgresql+psycopg://qa_workflow:REPLACE_ME@localhost:5432/qa_workflow'
export QA_FERNET_KEYS='REPLACE_ME_FERNET_KEY'
python -m worker.schema
python -m procrastinate --verbose --app=worker.app.app worker --name gen-1 --concurrency 3 --queues generation --wait
python -m procrastinate --verbose --app=worker.app.app worker --name review-1 --concurrency 6 --queues review,maintenance --wait
```

### Docker 一键部署

`deploy/compose.yaml` 提供 PostgreSQL、Alembic migration、知识库导入、Web、Procrastinate schema 初始化和 worker 服务。`deploy/env/*.example` 只含占位符；真实值由一键脚本生成到 gitignored 的 `deploy/env/local.env`：

```bash
cd deploy
./up.sh
# Windows PowerShell: .\up.ps1
```

该脚本会生成本地 env、构建镜像、启动 PostgreSQL、Alembic migration、`kb-migrate` 知识库导入、Web、Procrastinate schema 初始化、`worker-gen` 和 `worker-review`。首次创建账号不要把密码写进命令行参数；用 `docker compose --env-file env/local.env exec web python -m webapp.auth adduser <user> --password-env <ENV>` 或 `--password-file <file>` 注入。完整部署、备份、恢复演练、worker 故障演练和 50 并发 SSE 压测步骤见 `deploy/README.md`；演练报告写入 gitignored 的 `deploy/reports/`。

## PostgreSQL 知识库

当前知识库采用 **PostgreSQL 结构化真源 + Markdown 兼容渲染/导出**：

```bash
# 初始化 schema / 扩展（pgcrypto、citext、pg_trgm、vector）
python scripts/kb_store.py init

# 迁移 curated KB（自动排除 _kb/projects/*/_bulk-index/ 原始中间产物）
python scripts/kb_store.py migrate

# 查看状态与入库数量
python scripts/kb_store.py status

# DB → Markdown 导出，保持 validate-containers.py / kb-check-toc.py 等旧消费者可用
python scripts/kb_store.py export --product wms
```

默认连接 `qa_knowledge` 本机库；如需修改，优先设置 `QA_KB_DATABASE_URL`，或在 gitignored 的 `config/config.local.yaml` 写 `database:` 段。不要把数据库密码写入仓库。

兼容层验收命令：

```bash
python -m pip install -r webapp/requirements-test.txt
python -m pytest webapp/tests/test_admin.py webapp/tests/test_kb_store.py webapp/tests/test_kb_extract.py -q
python scripts/kb_store.py status
python scripts/kb-check-toc.py --product wms
python scripts/validate-containers.py <test-design.json 或工单目录> --product wms --quiet
```

如需让单元测试同时断言真实 PostgreSQL 迁移数量，先确认本机连接指向测试/本地库，再临时设置 `QA_KB_TEST_DB=1` 运行 `python -m pytest webapp/tests/test_kb_store.py -q`。

## 多产品线

`_kb/projects/<product>/` 和 `tickets/<product>/` 都按产品线分支。新增产品：
1. `mkdir _kb/projects/<new>` 加 `modules.md / rules.md / terms.md`
2. 在 `config/config.local.yaml` 的 `products:` 区追加该产品的 Jira project key 和 Confluence space key
3. 用 `/qa:new <new> <EAR-xxxx>` 即开始

## AI 工具

- **Claude Code**：原生支持，slash 命令 + MCP + CLAUDE.md
- **Codex CLI**：兼容，slash 命令不可用但可手动 `@prompts/01-analyze.md` 引用 prompt，AGENTS.md 等同 CLAUDE.md
- 后续可扩展：Cursor / Windsurf / 自研前端

## 备份到 GitHub

```bash
cd D:\Projects\qa-workflow
git init
git add .
git commit -m "init: qa-workflow scaffold"
git remote add origin git@github.com:<你的账号>/qa-workflow.git
git push -u origin main
```

`config/config.local.yaml` 已 gitignore，凭证不会进库。`tickets/` 默认入仓（如不想，编辑 `.gitignore` 取消注释）。

## 路线图

- **v1（当前）**：本地工作流 + Jira/Confluence MCP + Claude Code/Codex 双兼容
- **v2**：CodeArts API 适配器、接口测试参数化、Confluence 语义索引（本地向量缓存）
- **v3**：Web 前端（调用本工作流为后端 API）

## 抗幻觉契约（必读）

详见 [CLAUDE.md](./CLAUDE.md) 第 3 节。简言之：

1. AI **只能基于**已加载证据写用例：`_kb/` + 当前工单 `requirement.md` / `business-context.md` / `linked-issues.md` / `_jira-search.md` + Jira/Confluence MCP 实时拉取内容；其余一律视为未知
2. 任何文档未写明的字段/文案/流程，必须标 `[待确认]`，不许编造
3. Markdown 产物遵循 `_kb/_global/markdown-artifact-schema.md`，聊天完成报告不写入文件正文
4. 引用业务规则必须标章节号
5. 不为补字段而"看起来应该是"
