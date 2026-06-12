# qa-workflow v3 · Web 前端（webapp/）

> FastAPI + Jinja2 + HTMX 服务端渲染。把现有 `scripts/` 弱链 + 校验器 + 选单封装成
> 小团队多人可用的 Web 控制台。设计依据：`docs/frontend-implementation-plan.md`、
> 样式 `docs/DESIGN.md`、样张 `docs/ui-samples/dashboard.html`。当前采用 PostgreSQL 优先：
> 用户、凭证、作业、日志、知识库与工单产物镜像进数据库，Markdown/JSON 作为生成链兼容产物保留。

## 能做什么

- **Sprint 看板**：复刻样张——KPI、生成进度、工单处理状态表（格式/内容校验徽章、测试点、待确认）。
- **答题闸门**（核心）：解析 `questions.md` 三形态，结构化表单填答；保存=外科手术式只改「✅ 答案」区
  + 自动 normalize + validate 硬闸 + 乐观并发 + 原子写，绝不碰只读字段/人工已填。
- **只读用例树**：`test-design.json` 思维导图 + JSONPath 内联 FAIL/WARN + 可点问题清单定位；
  raw 字节编辑器（写原文、不经对象 round-trip，FAIL 阻断/WARN 放行）。结构化增改仍回 CodeArts。
- **弱链批量**：一键跑 `run_sprint.py`（选单/生成到答题停/答题后续跑/重跑），子进程实时日志。
- **强模型审计**：默认安装 Agent SDK，配置端点/登录态后服务端无头跑 spot-check / resolve；未配置则给「复制命令贴回 Claude Code」。
- **多人**：登录鉴权、单产品作业串行锁、操作者审计、按用户 Jira PAT。

## Docker 一键部署

```bash
cd deploy
./up.sh
# Windows PowerShell: .\up.ps1
```

脚本会生成 gitignored 的本地 env、启动 PostgreSQL、执行平台迁移、把仓库 `_kb` curated 知识库导入 PostgreSQL、启动 Web 和 worker。首次账号在容器内创建：

```bash
docker compose --env-file env/local.env exec web python -m webapp.auth adduser <用户名> --name <显示名> --role admin
```

## 快速开始

```bash
cd D:\Projects\qa-workflow

# 1. 建独立虚拟环境并装 web 依赖（不污染系统 Python）
python -m venv .venv
.venv\Scripts\activate            # Windows；macOS/Linux: source .venv/bin/activate
pip install -r webapp/requirements-web.txt

# 2. 建第一个账号（口令不回显）
python -m webapp.auth adduser linzixuan --name 林子宣 --role 测试工程师

# 3. 启动（默认 127.0.0.1:8800）
python -m webapp.main
#   或： uvicorn webapp.main:app --host 127.0.0.1 --port 8800

# 4. 浏览器打开 http://127.0.0.1:8800 → 登录
```

前提：仓库根已有 `config/config.local.yaml`（Jira/弱模型凭证），与跑 `scripts/` 时相同。

## 用户与凭证管理（CLI）

```bash
python -m webapp.auth list
python -m webapp.auth adduser <用户名> [--name 显示名] [--role 角色]
python -m webapp.auth passwd  <用户名>
python -m webapp.auth setpat  <用户名>      # 设该用户自己的 Jira PAT（不回显）
```

也可登录后在「配置」页设置自己的 Jira PAT / 改口令。**按用户 PAT**：登录用户的 PAT 会用于
其触发的只读 Jira 访问（选单、实时刷新）与弱链子进程（通过进程 env 覆盖，见
`scripts/_load_env.py` 的向后兼容增强）；未设置则回退到 `config.local.yaml` 的服务账号。

## 强模型（服务端无头审计）

`webapp/requirements-web.txt` 默认安装 `claude-agent-sdk`。要启用服务端强抽检/证据消解，在 `config/config.local.yaml` 配兼容 ANTHROPIC 的端点：

```yaml
ai:
  anthropic:
    base_url: "https://你的兼容端点/..."
    api_key:  "sk-..."
    default_model: "你的模型名"
```

配好后看板的「强抽检 / 证据消解」会在服务端运行；否则给复制命令兜底。上线前务必 shadow-run 平价评审（选 3-5 个已用 Claude Code 跑过的工单对比结果），再切流量。resolve 写回 `questions.md` 由确定性代码执行（只填空、不碰人工已填、写 `.bak`、写后校验回滚）。

## 内网部署（多人）

- 绑内网 host：`QA_WEBAPP_HOST=0.0.0.0 QA_WEBAPP_PORT=8800`，前面用 Nginx/Caddy 做 **TLS**。
- 启用安全 cookie：`QA_WEBAPP_SECURE_COOKIE=1`（仅经 HTTPS 传 cookie）。
- 会话密钥：默认持久化到 `webapp/data/secret.key`；可用 `QA_WEBAPP_SECRET` 覆盖。
- **单实例独占工作树**：所有写经该实例串行化；切勿多实例对多 clone（必然状态分裂）。
- systemd/NSSM 守护 `python -m webapp.main`。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `QA_WEBAPP_HOST` / `QA_WEBAPP_PORT` | 127.0.0.1 / 8800 | 绑定地址 |
| `QA_WEBAPP_SECRET` | 持久化随机 | 签名 cookie 密钥 |
| `QA_WEBAPP_SECURE_COOKIE` | 关 | 置 1 走 HTTPS-only cookie |
| `QA_WEBAPP_CONCURRENCY` | 4 | 弱链子进程默认并发 |
| `QA_WEBAPP_RELOAD` | 关 | 开发热重载 |

## 测试

```bash
pip install -r webapp/requirements-test.txt
pytest webapp/tests -q
```
覆盖：字节级 round-trip、raw 保存不经对象 round-trip、importlib 加载不污染 stdout、
questions 解析、算术沙箱 recompute 安全性。

## 目录

```
webapp/
├── main.py            FastAPI 装配 + 未登录跳转
├── config.py          路径/密钥/特性开关/强模型端点
├── auth.py            本地用户表 + 签名 cookie + 用户管理 CLI
├── deps.py            invoke() 错误隔离 + 按用户 env + 审计 + 模板
├── jobs.py            作业管理器（子进程弱链 + 异步强模型 + 串行锁）
├── services/          selection / tickets / questions / tree / jira / scripts_loader
├── strong/            schemas / tools(recompute) / runner(SDK) / spot_check / resolve
├── routers/           pages / ticket_actions / jobs
├── templates/         base/login/index/sprint/ticket/settings + partials/
├── static/            app.css（源自 DESIGN.md 令牌 + 样张）+ app.js
└── tests/
```

## 关键安全/正确性约束（实现已遵守）

- **密钥绝不进浏览器**：配置页只读掩码；PAT 仅服务端存（gitignored `data/users.json`）。
- **子进程 argv 列表**，永不 `shell=True`；注入用户 PAT 走 env。
- **算术维度用数值 AST 求值器**（`strong/tools.recompute`）取代裸 Bash，零代码执行风险。
- **乐观并发**：保存带读时 `mtime_ns`，不匹配 409，防批量 `--force` 覆盖人工编辑。
- **questions/JSON 保存** = 临时文件 → 校验 → `os.replace` 原子替换；FAIL 不落盘。
- `SystemExit`/`RuntimeError`（现有脚本的错误约定）一律经 `deps.invoke()` 隔离。
```
