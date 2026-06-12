# qa-workflow 部署说明

本目录提供平台化部署骨架：PostgreSQL、Alembic migration、Web 控制台、Procrastinate worker。

## 本地一键准备

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r webapp/requirements-web.txt
.venv/Scripts/python.exe deploy/init-local-env.py
```

`deploy/init-local-env.py` 会生成 gitignored 的 `deploy/env/local.env`，其中包含本机随机数据库密码、`QA_WEBAPP_SECRET` 和 `QA_FERNET_KEYS`。不要提交该文件。

## 本地 smoke

```bash
.venv/Scripts/python.exe deploy/smoke-local.py
```

该脚本验证：依赖导入、Fernet 加解密、Alembic 临时 SQLite migration、DB-backed 用户/凭证/session 登录链路、FastAPI `/healthz`、worker 模块导入。

## Docker Compose

```bash
cd deploy
python init-local-env.py

# 本地一键启动 PostgreSQL、迁移、Web、队列 schema 和拆分后的 worker
# 首次启动完成后访问 http://localhost:8800
docker compose --env-file env/local.env --profile worker up -d --build
```

创建首个账号时不要把密码写进命令行。推荐用环境变量或本地临时文件注入：

```bash
# PowerShell 示例：密码只进入当前 shell 进程环境，不写入仓库
$env:QA_BOOTSTRAP_PASSWORD = "请在本机输入临时密码"
docker compose --env-file env/local.env run --rm \
  -e QA_BOOTSTRAP_PASSWORD web \
  python -m webapp.auth adduser linzixuan --name 林子宣 --role admin --password-env QA_BOOTSTRAP_PASSWORD
Remove-Item Env:\QA_BOOTSTRAP_PASSWORD
```

启用 `worker` profile 时，Compose 会先执行 Alembic migration，再执行幂等 Procrastinate schema 初始化，并启动两个队列 worker：

- `worker-gen` 只监听 `generation`，由 `QA_GENERATION_WORKER_CONCURRENCY` 控制弱模型生成并发。
- `worker-review` 监听 `review,maintenance`，由 `QA_REVIEW_WORKER_CONCURRENCY` 控制强模型复核和维护任务并发。
- legacy 聚合 worker 保留在 `legacy-worker` profile，仅用于排障回退：`docker compose --env-file env/local.env --profile legacy-worker up worker`。

`deploy/init-local-env.py` 默认写入 `QA_USE_WORKER=1` 和 `QA_USE_DB_ARTIFACTS=1`，Web 会入队，worker 执行生成/强检查并写回平台 DB。Web 默认暴露 `http://localhost:8800`，可用 `QA_WEBAPP_PORT` 改宿主机端口，并通过 `/healthz` 做容器健康检查；`/healthz` 同时返回 `queue_depth`、`queue_depth_warn` 和 `queue_depth_threshold`，生产监控可按 `QA_QUEUE_DEPTH_WARN` 告警。平台库连接预算默认按单进程 `QA_DB_POOL_SIZE=5`、`QA_DB_MAX_OVERFLOW=5`、`QA_DB_POOL_RECYCLE=3600`，worker/队列连接默认 `QA_QUEUE_POOL_MIN_SIZE=1`、`QA_QUEUE_POOL_MAX_SIZE=2`、`QA_QUEUE_POOL_MAX_LIFETIME=3600`；多实例部署前按 `进程数 × 池上限` 复核 PostgreSQL `max_connections`。Jira REST 默认走进程内令牌桶：`QA_JIRA_RATE_QPS=1`、`QA_JIRA_RATE_BURST=5`；平台库可用时，`scripts/jira_fetch.py` 会把 issue JSON 快照写入 `tickets.metadata`，并按 `QA_JIRA_CACHE_TTL_SECONDS=900` 复用，降低关联工单和批量选单请求放大。新 Docker 环境默认启用 P3a 物化执行：DB artifact → `.work/<run>/tickets` → 子进程/强模型 → 白名单产物回灌 DB → 当前用户 `userdata` 兼容缓存；仅排障回退时把 `QA_USE_DB_ARTIFACTS=0` 改回 P2 文件模式。生产部署建议放在反向代理后，并设置 `QA_WEBAPP_SECURE_COOKIE=1`。

手动备份当前平台库可运行：

```bash
docker compose --env-file env/local.env --profile backup run --rm backup
```

备份文件写入 Compose `backups` volume。恢复演练可在同一 Compose project 内创建随机临时库并验证核心表；默认不删除临时库，报告会写入 gitignored 的 `deploy/reports/`：

```bash
python restore-drill.py --env-file env/local.env --create-backup
```

确认报告后，如需自动清理本次生成的 `qa_restore_drill_*` 临时库，再显式加 `--drop-temp-db-after`。脚本会拒绝操作非演练库名，避免误删业务库。

## 运维演练

worker 故障演练默认只生成计划报告，不会停容器；真正演练时显式加 `--execute`，脚本会停止指定 worker、验证 `/healthz` 仍可访问、再拉起并等待恢复：

```bash
python worker-failure-drill.py --env-file env/local.env --service worker-gen
python worker-failure-drill.py --env-file env/local.env --service worker-gen --execute
```

50 并发 SSE 压测需要一个已有作业 `job_id`（进行中作业可测长连，已结束作业可测端点并发握手），并通过当前 shell 环境变量注入登录凭证；报告只保留聚合延迟和连接统计，不写 cookie 或密码：

```bash
$env:QA_LOAD_USERNAME = "你的账号"
$env:QA_LOAD_PASSWORD = "当前 shell 临时密码"
python sse-load-check.py --base-url http://127.0.0.1:8800 --job-id <job_id> --connections 50 --duration 15
Remove-Item Env:\QA_LOAD_USERNAME
Remove-Item Env:\QA_LOAD_PASSWORD
```

## 注意

- `deploy/env/*.example` 与 `deploy/secrets/*.example` 只允许占位符；真实值写入 gitignored 的 `deploy/env/local.env`、部署机本地 secret 文件或平台密钥管理系统。
- `deploy/secrets/README.md` 仅预留生产 hardening 入口；当前本地一键部署默认仍使用 `local.env`，避免新环境首次启动还要额外配置 secrets。
- `tickets/`、`userdata/`、`webapp/data/*.json`、`webapp/data/*.key`、`.claude/`、`.mcp.json`、真实 `deploy/secrets/*` 不进入镜像上下文。
- `QA_USE_WORKER=1` 时 worker 是生成/复核执行真源；如需本地调试回退，可临时设为 `0`，让 Web 线程直接执行。
