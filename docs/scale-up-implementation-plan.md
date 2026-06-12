# qa-workflow 规模化落地实施方案

> 版本：**v1.1**（2026-06-11）
> v1.0 → v1.1 修订：经三视角对抗式审查（代码事实 / 可行性排期 / 安全运维，3 blocker + 13 major 全部采纳）。主要变更：① 执行模型改为「P2 文件模式共享卷 → P3 物化目录」消除阶段环形依赖；② 修正 `run_sprint.py relative_to(REPO_ROOT)` 崩溃点与逐步增量回灌；③ 新增 Procrastinate 僵死作业清扫与 worker 凭证白名单注入设计；④ 连接数按进程重算；⑤ 覆盖账本改事务性认领；⑥ 派生列对齐真实看板徽章契约；⑦ CSRF 双通道、限速防自锁、按服务拆分 secrets；⑧ Apifox webhook 改为默认轮询；⑨ 排期拆 P3a/P3b 并补容量模型、第二真实产品、用户培训等缺项。
>
> 目标：把当前「单人单机 · 文件真源 · 单进程」的 qa-workflow 改造为可供 **300–500 名 QA 工程师**使用的内部平台。
> 已定决策：**保留 FastAPI + Jinja2 + HTMX**；全量迁 PostgreSQL；队列用 **Procrastinate**；登录接 **钉钉**；部署 **Linux + Docker Compose**；产品线通用化；后续接入 **Apifox 接口测试**（两轨）。
> 依据：4 轮代码逐文件审计 + 6 轮联网调研（2025–2026 一手资料）+ 3 轮对抗式审查；`file:line` 对应 2026-06-10 工作区。⚠️ = 查证过的硬约束。

---

## 0. 如何读这份文档

- **§1** 一页纸总览；**§11** 按周排期——先读这两节。
- **§2–§10** 各专题施工图，每节自带「验收标准」。
- **§13** 是需要你拍板的事项清单（已扩充到 8 项）。

---

## 1. 总览

### 1.1 设计原则

1. **不换栈，换地基**：Web 层与「校验器闸门 + 强模型对抗复核」机制全保留；换的是存储（文件→PG）、作业模型（内存线程→持久队列）、身份（users.json→钉钉 SSO + DB）。
2. **PostgreSQL 是唯一有状态依赖**：业务数据、任务队列、会话、审计全在一个 PG——一个备份故事、一个事务边界，不引 Redis/RabbitMQ。
3. **字节契约神圣不可侵犯**：`test-design.json` 是逐字节粘进华为云 CodeArts 的；入库后以**原文文本**存储，永不对象 round-trip（`webapp/services/tree.py:169-196` 的纪律延续到 DB 层）。
4. **渐进迁移**：每阶段独立可上线、可回滚；旧脚本管线分两步接入——**P2 文件模式（共享卷，零改造）→ P3 物化目录（DB 真源）**，不存在「还没入库就要从库里导出」的环形依赖。
5. **产品即配置**：第二条产品线接入 = 配置 + KB 骨架，不改引擎代码（但 KB 内容建设是真实人力成本，排期单列，见 §11）。
6. **最小权限贯穿**：用户凭证 Fernet 加密入库、worker 子进程只拿白名单 env、容器按职责拆分 secrets、所有查询 owner-scoped。

### 1.2 目标架构

```
                        员工浏览器 / 钉钉客户端(免登)
                                   │ HTTPS
                          ┌────────▼────────┐
                          │  Caddy (TLS/H2) │  ← 内网域名；H2 对 SSE 多连接友好
                          └────────┬────────┘
                                   │ X-Forwarded-For（uvicorn --proxy-headers 信任 Caddy 网段）
                  ┌────────────────▼─────────────────┐
                  │  web 容器（uvicorn 4 workers）      │
                  │  - 钉钉 OAuth/免登 + 会话(DB)       │
                  │  - 看板/答题/树/管理（owner-scoped）  │
                  │  - defer 任务（Procrastinate sync）  │
                  │  - SSE(async)/轮询读 job_logs 表     │
                  └────────────────┬─────────────────┘
                                   │ SQL (psycopg3, 每进程小池)
        ┌──────────────────────────▼──────────────────────────┐
        │              PostgreSQL 16 (+pgvector)               │
        │  users/sessions/credentials/products/tickets/         │
        │  artifacts/pipeline_runs/steps/job_logs/ledger/audit  │
        │  + kb_*（对齐）+ procrastinate_*（队列+僵死清扫）        │
        └───────┬──────────────────────────────┬───────────────┘
                │ LISTEN/NOTIFY                 │
   ┌────────────▼────────────┐     ┌────────────▼────────────┐
   │ worker-gen × N           │     │ worker-review × 1        │
   │ queue=generation (sync)  │     │ queue=review,maintenance │
   │ P2: 共享 userdata 卷直跑  │     │ 强模型复核/预答/KB 回填    │
   │ P3: 物化 .work/<run>/ →  │     │ + retry_stalled_jobs 清扫 │
   │ 子进程(白名单 env) → 增量回灌│     │ + Apifox spec 轮询        │
   └────────────┬────────────┘     └────────────┬────────────┘
                │ 出网（唯一公网依赖为出站 HTTPS）     │
        百炼 qwen / 强模型端点 / Jira / api.dingtalk.com / api.apifox.com
```

### 1.3 技术选型与版本锁定（2026-06-11 核实）

| 组件 | 选型 | 版本锁定 | 备注 |
|---|---|---|---|
| Web | FastAPI | `>=0.136.3,<0.137` | 原生 SSE 自 0.135.0（官方文档确认） |
| ORM/迁移 | SQLAlchemy 2.0 + Alembic | `>=2.0.50,<2.1`、`alembic>=1.18.4` | 同步会话；2.1 仍 beta 不用 |
| 驱动 | psycopg3 | `psycopg[binary,pool]>=3.2` | 全仓唯一驱动 |
| 队列 | **Procrastinate** | `>=3.8.1,<4` | 原生 per-key lock、sync 任务、abort、cron；⚠️ 需自配僵死作业清扫（§4.3）；PGQueuer 1.0 async-only 且无 per-key 串行，落选 |
| 加密 | cryptography Fernet/MultiFernet | `>=48.0.1` | 应用层加密；不用 pgcrypto（密钥进 SQL 文本） |
| 前端 | HTMX 2.x（锁定）+ Tailwind + DaisyUI | — | 静态资源全本地化；htmx 4 GA 后再评估 |
| 反代 | Caddy 2 | stable | 自动 TLS/H2 |
| 数据库 | PostgreSQL 16 | `pgvector/pgvector:pg16` | trgm/citext/pgcrypto 内置 |

---

## 2. 仓库重组（治「文件太多、没分类」）

### 2.1 现状诊断

根目录 23 个顶层条目；`scripts/` 24 个文件混着 16 个核心引擎、5 个一次性 KB 建库脚本；1.1GB `_bulk-index/` 原始抓取物在工作树里；`archive/` 空壳；根 `tickets/` 已空但仍是 4 处代码默认兜底；`webapp/data/ownership.json` 运行态却被 git 跟踪；`.gitignore` 无锚 `tickets/` 过宽；README 目录说明已脱节。

### 2.2 硬约束（动文件前必读）

⚠️ 逐条来自代码清点，违反即静默断链：

1. `scripts/` 是扁平 sys.path 裸 import 网（qa_pipeline ↔ cheap_model/jira_fetch/batch_generate/kb_store 等）；
2. 连字符校验器只能按路径 importlib 加载（`scripts_loader.py:60-76`），被 `batch_generate.run_validator` 子进程统一调用；
3. REPO_ROOT 深度契约：scripts 侧 8+ 处 `parent.parent`、webapp 侧 `WEBAPP_DIR.parent`——`scripts/`、`webapp/` 必须保持一级子目录；
4. `config/`、`prompts/`、`_kb/` 目录名被硬编码字符串引用，**不改名**；
5. `.claude/`、`.mcp.json`、`CLAUDE.md`、`AGENTS.md` 位置是 harness 约定，不动；
6. 工单四层目录 `<root>/<product>/<sprint>/<KEY>/` 是契约（`batch_generate.py:168,259` 用 `parent.parent.name` 推产品）；
7. ⚠️ **`run_sprint.py:138,248,262` 对工单路径调 `.relative_to(REPO_ROOT)`**——`QA_TICKETS_ROOT` 指到仓库外必抛 `ValueError`（今天能跑只因 userdata 在仓库根下）。对 §7.2 物化方案是硬约束：**物化目录必须放在仓库根之下（`<repo>/.work/`），且 P3 前给这三处打 `os.path.relpath` 容错补丁 + 回归测试**（设 `QA_TICKETS_ROOT` 为仓库外 tmp 跑通）。

### 2.3 目标目录结构（终态）

```
qa-workflow/
├── CLAUDE.md  AGENTS.md  README.md          # 位置不动；内容按 §6.3 拆分瘦身
├── .claude/  .mcp.json                       # 不动
├── config/  prompts/  _kb/                   # 名字不动
├── core/                                     # ★引擎包（scripts 逐步迁入）
│   ├── productcfg.py  llm/  jira/  pipeline/  validators/  store/  dingtalk/
├── scripts/                                  # 薄 CLI 壳 + mcp-atlassian-wrapper.py
├── tools/kb-bootstrap/                       # 一次性建库脚手架（新产品复用）
├── webapp/                                   # 包名不动；services 改读 core/store
├── worker/                                   # Procrastinate app + tasks
├── deploy/                                   # Dockerfile/compose/Caddyfile/alembic/备份脚本
├── docs/  + docs/history/                    # 设计期产物归档
├── archive/                                  # 冷数据
├── .work/                                    # ★P3 起：物化工作区（gitignore，可清重建）
└── userdata/                                 # P2 共享卷挂载点；P3 后只作导出兼容
```

### 2.4 分两步执行

**R1 低风险清理（半天，立即可做）**：

| 动作 | 对象 | 注意 |
|---|---|---|
| 移动 | 5 个 KB 建库脚本 → `tools/kb-bootstrap/` | 其内部 `parent.parent` 推 REPO_ROOT，挪深一层需同步改推导 |
| 移动 | `docs/{stitch-design-brief.md, ui-samples*}` → `docs/history/` | ⚠️ `webapp/README.md:4-5,109` 链接 `DESIGN.md`/`ui-samples/dashboard.html`/`frontend-implementation-plan.md`——**同一提交更新链接**；`DESIGN.md` 暂留 docs/ 原位（§9 Tailwind 主题要用），§9 完成后再归档 |
| 移出工作树 | `_bulk-index/`（1.1GB）→ 仓库外冷存储 | gitignored，无主链依赖 |
| 归档 | `之前我的用例（示例）/`、`_kb/projects/wms/_backup/` → `archive/` | 冷数据 |
| 删除 | `_kb/projects/dbonly/`（空）、孤儿 pyc、`.pytest_cache` | 先跑测试确认 dbonly 无依赖 |
| git 修正 | `git rm --cached webapp/data/ownership.json`；`.gitignore`：无锚 `tickets/` → 锚定 `/tickets/` + `/userdata/` + `/.work/`，显式忽略 `webapp/data/*.json`（保留 example） | 改完 `git status` 全量核对 |
| 文档 | 重写 README 目录结构段 | — |

**R2 包化（伴随 P2/P3，不单独排期）**：新代码只进 `core/`、`worker/`；`scripts/` 旧引擎 P2 不动（worker 文件模式直跑）；P3 迁 `core/` 后留同名薄壳；连字符校验器迁入时下划线化、原名留转发壳，`scripts_loader`/`run_validator` 同步改。

**验收**：R1 后 `pytest webapp/tests -q` 全绿、`run_sprint --dry-run` 正常、slash 命令可用、webapp/README 链接无死链；根目录顶层条目 ≤ 15。

---

## 3. PostgreSQL 全量数据模型

### 3.1 迁移范围与原则

| 现状 | 去向 | 阶段 |
|---|---|---|
| users.json（明文凭证、无锁读改写） | users + user_credentials（加密）+ sessions | **P1** |
| 产品清单（config products 段） | products 表（jsonb config；YAML 作种子） | **P1**（⚠️ 提前——P2 的 pipeline_runs FK 需要它） |
| jobs.py 内存作业 | pipeline_runs/steps/job_logs + procrastinate_* | **P2** |
| 工单产物文件树 | tickets + ticket_artifacts（原文）+ 派生列 | **P3a** |
| .sprint-state（账本/选单/进度） | coverage_ledger（事务认领）+ sprint_selections | **P3a** |
| audit.log | audit_events | P1 起新事件入库，旧文件一次性导入 |
| ownership.json | 不迁（已被按用户根取代，仅测试引用）；语义由 tickets.owner_user_id 承接 | — |
| _kb/（已在 PG） | 保留 kb_*，修连接池/async 阻塞/静默回退（§3.5） | P3a |
| config.local.yaml | 系统级凭证留 env；products 段上移 DB | P1 |

原则：单库（业务表与 kb_*、procrastinate_* 同库，跨域一次 COMMIT）；uuid PK/timestamptz/产品 FK 沿用 kb_store 约定；⚠️ 大文本单独窄表防 TOAST 误扫；派生列只读、由产物写入口统一刷新。

### 3.2 表设计（DDL 草案）

**身份域**

```sql
CREATE TABLE users (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username         text NOT NULL UNIQUE,
  display_name     text NOT NULL DEFAULT '',
  role             text NOT NULL DEFAULT 'qa' CHECK (role IN ('admin','qa','viewer')),
  dingtalk_userid  text UNIQUE,      -- 免登路径直接得到；工作通知收件人
  dingtalk_unionid text UNIQUE,      -- 扫码路径 /me 得到
  avatar_url       text NOT NULL DEFAULT '',
  mobile           text NOT NULL DEFAULT '',
  -- ⚠️ 必填闸门：钉钉姓名 ≠ Jira「问题测试员」displayName。无 display_name 回退！
  -- 未设置且未通过探针校验前，选单/生成功能禁用并引导到设置页（防 JQL 静默 0 单）
  jira_tester_name text NOT NULL DEFAULT '',
  jira_tester_verified_at timestamptz,          -- 设置页「校验 Jira 身份」探针通过时间
  jira_url         text NOT NULL DEFAULT '',
  pwd_salt text,  pwd_hash text,                -- 仅 admin 兜底账号
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),  last_login_at timestamptz
);

CREATE TABLE user_credentials (                  -- 仅「用户级」凭证；系统级凭证见下方说明
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('jira_pat','ai_weak','ai_strong')),
  provider text NOT NULL DEFAULT '',  base_url text NOT NULL DEFAULT '',  model text NOT NULL DEFAULT '',
  ciphertext text NOT NULL,                      -- Fernet token
  key_version smallint NOT NULL DEFAULT 1,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, kind)
);
-- ⚠️ 系统级凭证（钉钉 Client Secret、Apifox 服务 token、DB 密码）一律走 env/compose secrets，
--    不入 user_credentials（行级语义+级联删除不适配系统凭证）。如确需库存，另建无 user FK 的
--    service_credentials 表——默认不建。

CREATE TABLE sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash text NOT NULL UNIQUE,               -- cookie 只放不透明随机串
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),  expires_at timestamptz NOT NULL,
  ip inet,  user_agent text NOT NULL DEFAULT '',  revoked_at timestamptz
);
```

**产品域**（P1 建）

```sql
CREATE TABLE products (
  key text PRIMARY KEY,  display_name text NOT NULL DEFAULT '',
  config jsonb NOT NULL DEFAULT '{}',            -- §6.1 schema 全量
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),  updated_at timestamptz NOT NULL DEFAULT now()
);  -- 创建时同步 upsert kb_products（kb_* 的 FK 锚点）
```

**工单与产物域**（P3a 建）

```sql
CREATE TABLE tickets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product text NOT NULL REFERENCES products(key),
  sprint_date date NOT NULL,
  ticket_key text NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES users(id),
  summary text NOT NULL DEFAULT '',
  feature_key text NOT NULL DEFAULT '',
  -- 派生徽章（⚠️ 对齐 tickets.py:133-217 + questions.py:144-149 的真实渲染契约，
  --   单一 review_status 字符串不够）：
  has_design boolean NOT NULL DEFAULT false,
  has_questions boolean NOT NULL DEFAULT false,
  has_spotcheck boolean NOT NULL DEFAULT false,
  json_ok boolean,  json_warn int NOT NULL DEFAULT 0,  json_fail int NOT NULL DEFAULT 0,
  testpoints int NOT NULL DEFAULT 0,  todo_marks int NOT NULL DEFAULT 0,
  q_total int NOT NULL DEFAULT 0,  q_human int NOT NULL DEFAULT 0,
  q_auto  int NOT NULL DEFAULT 0,  q_pending int NOT NULL DEFAULT 0,
  review_needs_action boolean NOT NULL DEFAULT false,
  review_summary text NOT NULL DEFAULT '',
  badge jsonb NOT NULL DEFAULT '{}',             -- 其余细粒度布尔组(failed/unfixable/leftover/…)整包存放
  needs_resume boolean NOT NULL DEFAULT false,   -- questions 晚于 design 的「需续跑」信号
  created_at timestamptz NOT NULL DEFAULT now(),  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, product, sprint_date, ticket_key)
);
CREATE INDEX tickets_board_idx ON tickets (owner_user_id, product, sprint_date);

CREATE TABLE ticket_artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ticket_id uuid NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  name text NOT NULL,
  rev int NOT NULL DEFAULT 1,                    -- 乐观锁（替代 mtime_ns；不匹配=409）
  size_bytes int NOT NULL,  content_hash text NOT NULL,
  updated_by uuid REFERENCES users(id),  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ticket_id, name)
);
CREATE TABLE ticket_artifact_blobs (             -- 正文隔离（TOAST）
  artifact_id uuid PRIMARY KEY REFERENCES ticket_artifacts(id) ON DELETE CASCADE,
  content text NOT NULL                          -- ⚠️ 原文逐字节；JSON 也存 text 不存 jsonb
);
CREATE TABLE ticket_artifact_revisions (         -- 替代 *.bak（保留最近 N 版，cron 清理）
  id bigserial PRIMARY KEY,
  artifact_id uuid NOT NULL REFERENCES ticket_artifacts(id) ON DELETE CASCADE,
  rev int NOT NULL,  content text NOT NULL,
  saved_by uuid,  saved_at timestamptz NOT NULL DEFAULT now()
);
```

**作业与流水线域**（P2 建）

```sql
CREATE TABLE pipeline_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text NOT NULL,            -- sprint_generate|strong_review|resolve|finalize|kb_extract|api_testgen|…
  product text NOT NULL REFERENCES products(key),
  sprint_date date,
  owner_user_id uuid NOT NULL REFERENCES users(id),
  status text NOT NULL DEFAULT 'queued'
         CHECK (status IN ('queued','running','awaiting_input','succeeded','failed','cancelled')),
  current_step text NOT NULL DEFAULT '',
  params jsonb NOT NULL DEFAULT '{}',  result jsonb NOT NULL DEFAULT '{}',
  procrastinate_job_id bigint,  label text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),  started_at timestamptz,  finished_at timestamptz
);
CREATE INDEX runs_recent_idx ON pipeline_runs (owner_user_id, product, created_at DESC);

CREATE TABLE pipeline_steps (
  run_id uuid NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
  ticket_key text NOT NULL DEFAULT '',
  step text NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  result jsonb NOT NULL DEFAULT '{}',
  started_at timestamptz,  finished_at timestamptz,
  PRIMARY KEY (run_id, ticket_key, step)
);

CREATE TABLE job_logs (
  run_id uuid NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
  seq bigint NOT NULL,  ts timestamptz NOT NULL DEFAULT now(),  line text NOT NULL,
  PRIMARY KEY (run_id, seq)
);  -- ⚠️ 写入前过 secret 扫描（sk-/token 形态正则脱敏）；终态 30 天后归档清理
```

**账本与选单域**（P3a 建；P2 期间账本仍是各用户文件，全局去重 P3a 才生效——过渡期局限要写进试点须知）

```sql
CREATE TABLE coverage_ledger (
  product text NOT NULL REFERENCES products(key),
  feature text NOT NULL,
  status text NOT NULL DEFAULT 'claimed' CHECK (status IN ('claimed','covered')),
  covered_by text NOT NULL DEFAULT '',
  sprint_date date,  platform text,
  source text NOT NULL DEFAULT 'run' CHECK (source IN ('run','scan','claim')),
  owner_user_id uuid REFERENCES users(id),
  claimed_at timestamptz NOT NULL DEFAULT now(),  covered_at timestamptz,
  PRIMARY KEY (product, feature)
);
```

⚠️ **并发去重靠「事务性认领」而非快照**（审查确认快照模式有竞态：两个用户同时选单会同时选中同一未覆盖特性、双倍花钱）：选单（plan）阶段对每个拟跑特性 `INSERT … ON CONFLICT DO NOTHING` 抢占 `claimed` 行——抢不到的在选单报告标「他人进行中，跳过」；run 成功 → `covered`；run 失败/取消 → 删除本 run 的 claim（maintenance 对账任务兜底清理超时 claim）。`build_coverage` 的目录扫描源（`select_sprint.py:314-349`）在 P3a 改为直接查本表。**产品决策**：默认全局去重；若要保留「各管各的」，PK 加 owner_user_id（改动一行）。

```sql
CREATE TABLE sprint_selections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product text NOT NULL,  sprint_date date NOT NULL,
  owner_user_id uuid NOT NULL REFERENCES users(id),
  payload jsonb NOT NULL,                        -- decisions/run_list/jql/sprint_meta 原样
  generated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, product, sprint_date)
);

CREATE TABLE audit_events (
  id bigserial PRIMARY KEY,  ts timestamptz NOT NULL DEFAULT now(),
  actor text NOT NULL DEFAULT '',  action text NOT NULL,
  product text NOT NULL DEFAULT '',  object_kind text NOT NULL DEFAULT '',
  object_id text NOT NULL DEFAULT '',  detail jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_ts_idx ON audit_events (ts DESC);
```

### 3.3 SQLAlchemy / Alembic / 连接预算

- `core/store/db.py`：同步 engine + Session；`Base.metadata` 配 naming_convention；模型全量 import 进 `alembic/env.py`；autogenerate 必人工审查；`alembic upgrade head` 由独立 migrate 服务执行（§10.1），不在应用启动逻辑里。
- ⚠️ **连接预算按「每进程 × 进程数」算**（v1.0 算错 4 倍，已修）：

| 来源 | 配置 | 连接数 |
|---|---|---|
| web：uvicorn 4 workers × engine(pool_size=5, max_overflow=5) | 每进程 ≤10 | ≤40 |
| web：4 × Procrastinate SyncPsycopgConnector(min=1,max=2) | 每进程 ≤2 | ≤8 |
| worker-gen / worker-review（async pool 各 ~10） | — | ~20 |
| migrate/backup/运维 | 瞬时 | ~5 |
| **合计** | | **≈70 < max_connections=100** |

  触发 PgBouncer 的条件写死：web 实例 >1 台或合计预算 >80。
- 所有 engine 带 `pool_pre_ping=True, pool_recycle=3600`。

### 3.4 数据导入与割接（多用户版）

- 一次性脚本 `scripts/migrate_userdata_to_pg.py`：遍历 `userdata/<user>/tickets/…` → tickets+artifacts（sha256 回读核对）；`.sprint-state` → ledger/selections；users.json → users+credentials（读出即加密，**完成后钉钉通知用户轮换**）；audit.log → audit_events（best-effort）。`--dry-run` 出核对清单；正式跑前 tar 快照。
- ⚠️ **割接程序按多用户设计**（审查指出：P3a 时已有试点用户在写文件，单用户窗口假设失效）：**按用户逐个割接**——冻结该用户写入（webapp 横幅+禁写 5–15 分钟）→ 导入 → 字节核对 → 该用户切 DB 读写 → 解冻。P3a 验收含一次完整割接演练（演练用户=开发者自己）。
- 回滚：保留 `export_ticket_dir`（物化机制子集）——DB 故障可整树导出旧目录结构，旧版镜像立即可用。

### 3.5 kb_store 三项修正（新表不准复制的旧习惯）

1. 每操作新建连接 → psycopg_pool / 共享 engine；
2. async 路由调同步 store 阻塞事件循环 → admin 路由改 `def`（进线程池）；
3. DB 失败静默回退文件 → 保留回退但记 audit + 页面黄条「KB 处于文件回退模式」。
4. 中文检索：pg_trgm 保留 + 既有 pgvector chunks 增量启用；不引 zhparser/pg_jieba。

**§3 验收**：导入干跑/实跑字节核对 100%；割接演练单用户 ≤15 分钟且可回退；看板全走派生列后首页 P95 < 300ms（50 并发）；并发保存设置无丢更新；**用户 A 枚举/读取/取消用户 B 的工单、产物、run、日志一律 403**（owner-scoped 全局规则测试集）。

---

## 4. 并发与作业系统（Procrastinate）

### 4.1 选型结论（不变）

`lock=f"{user_id}:{product}"` 原生 per-key 严格串行；sync 任务原生支持；`queueing_lock` 防连点；`cancel(abort=True)` + `should_abort()` 可取消；`@app.periodic` 定时。PGQueuer async-only 且无 per-key 串行，落选。

### 4.2 队列拓扑与凭证流

| 队列 | 任务 | 容器 | 并发 | 锁 |
|---|---|---|---|---|
| generation | 弱链生成（sync 子进程） | worker-gen | 由 §4.6 容量模型定（起步 3） | lock+queueing_lock = `user:product` |
| review | 强模型复核/预答/finalize/kb-extract（async） | worker-review | 6 | 同上 |
| maintenance | **retry_stalled_jobs 清扫**、claim 对账、Apifox spec 轮询、日志/修订版清理、PAT 轮换提醒 | worker-review 兼跑 | 1 | — |

- web 进程：`SyncPsycopgConnector` 只 defer/cancel（官方限制恰合分工）；worker：async `PsycopgConnector`。⚠️ 不用 `SQLAlchemyPsycopg2Connector`（psycopg2-only）。

⚠️ **worker 凭证流（v1.1 新增，blocker 修复）**——现行 `webapp/deps.py:93-117 subprocess_env()` 是 `dict(os.environ)` 整体复制 + 明文注入，迁到 worker 容器后会把 Fernet 主密钥、钉钉 Secret、DB 密码全部灌进跑不可信内容的子进程，**禁止沿用**：

1. 任务体按 `run.owner_user_id` 查 `user_credentials`，**任务内瞬时解密**；
2. 子进程 env 用**白名单从零构造**：`JIRA_URL/JIRA_PERSONAL_TOKEN/CHEAP_MODEL_*/QA_TICKETS_ROOT/QA_SELECT_TESTER/PATH/LANG` 等运行必需项——绝不 `dict(os.environ)` 继承；
3. `QA_SECRET_KEY`、`DINGTALK_*`、`APIFOX_TOKEN`、`QA_DATABASE_URL` 等系统机密**永不进入子进程环境**（worker 主进程持有即可）；
4. `job_logs` 写入前过脱敏正则（`sk-\w+`、PAT 形态、`api_key=` 等→`***`）；`argv_display` 同样过滤。

### 4.3 作业生命周期（按真实「草稿先行·只答一次」流程修正）

⚠️ v1.0 此节与代码不符（审查证据：`webapp/routers/jobs.py:24-29`、`run_sprint.py:228-234`），按现行 webapp 编排重写：

```
用户点「生成」（前置：jira_tester_verified_at 非空，否则引导设置页）
 └ web: INSERT pipeline_runs(queued) + generate_task.configure(lock=u:p, queueing_lock=u:p).defer()
worker-gen:
 ├ runs→running；选单认领特性（coverage_ledger 事务 claim，§3.2）
 ├ run_sprint --select --until draft          ← ⚠️ 必须 --select（账本登记）+ 停在 draft（产出 _draft-design.json）
 ├ 每完成一个工单：该单产物增量回灌 PG + pipeline_steps checkpoint（P3a 起；P2 为文件模式无需回灌）
 └ 成功 → 同锁 defer review 任务
worker-review:
 ├ resolve（证据消解预答）+ draft_review（草稿复核折问进 questions.md）
 └ runs→awaiting_input                        ← job 正常结束，不占 worker、不占锁
用户在 webapp 只答一次（rev 乐观锁 + normalize/validate 闸门）
 └ web: defer resume 任务（同锁）
worker-gen: run_sprint --select --resume-after-questions（points/design/packet + 账本 source=run）
 └ 成功 → 同锁 defer finalize（repair 结构修复 + spot_check 语义复核）→ succeeded → 钉钉通知
```

⚠️ **僵死作业清扫（blocker 修复）**：Procrastinate 官方文档明确——worker 被 kill -9 后 `doing` 作业**永不自动恢复**，且持续占有锁把该用户该产品的后续作业无限期堵死。必须：

1. maintenance 队列加周期任务（`@app.periodic(cron="*/10 * * * *")` + queueing_lock）调 `get_stalled_jobs()` + `retry_job()`（官方推荐模式）；
2. 配 `update_heartbeat_interval` / `stalled_worker_timeout`；
3. 同任务做 run 对账：stalled 的 `doing` → pipeline_runs 标 `failed`（UI 给「重跑」按钮），孤儿 claim 释放。

**幂等与费用保护**：P3a 起产物**按工单增量回灌**（不是 run 结束一次性回灌——审查确认那样 kill -9 会丢全部中间产物、重跑全额重付 LLM 费用）；`.work/<run_id>/` 放**命名卷**，重试时按 run_id 复用既有工作目录，skip-if-exists（`qa_pipeline.py:598-652` 的文件存在性判断）自然命中。

### 4.4 进度展示

- 第一步：HTMX 轮询读 `job_logs (run_id, seq>last)`——模板几乎不改。
- 第二步：作业详情页 SSE。⚠️ **SSE 端点必须 async def**（async 轮询 DB / LISTEN-NOTIFY）：审查确认若用 `def` 生成器，每条长连独占线程池 1 线程（AnyIO 默认 ~40/进程），周三高峰几十人盯作业页即可拖死全部 sync 路由。另：每用户 SSE 并发上限 3，超出降级轮询；压测验收含「50 条并发 SSE 下普通页面 P95」断言。

### 4.5 钉钉工作通知

终态时 `asyncsend_v2`（action_card 带跳转）；⚠️ 同内容同人一天只发到一次且**静默丢弃**——正文必须带 run_id/时间戳；errcode=0 ≠ 送达，task_id+getsendresult 兜底；msg ≤2048 字节。

### 4.6 容量模型（v1.1 新增——审查认定为全方案最大风险假设，P2 前必须完成）

- 公式：周三峰值需求 ≈ 活跃用户数 × 人均 sprint 工单数 × 单工单弱链耗时（实测中位数）；供给 ≈ worker-gen 并发 × 内部并发（`run_sprint --concurrency`，现默认 4）；**真上限大概率是百炼共享 key 的配额**（多数用户不自配 key 时回退全局 key，`deps.py:109-130`）。
- 行动：① 找百炼控制台核实团队配额（QPM/TPM/日额度）→ 写进 §13 待确认；② 用现网历史数据测单工单耗时分布；③ 定 SLO（建议「周三 18:00 前提交的批次 24h 内出稿」）；④ 据此定 worker-gen 副本数与排队深度告警阈值；⑤ 错峰策略：选单允许「定时执行」（maintenance defer with schedule_at）摊平峰值。
- **Jira 侧放大**（审查新增）：每单全量关联拉取 + ≥5 轮 JQL × 几百人×每周——必须加 worker 层 Jira 全局限速（令牌桶）+ issue 快照缓存（tickets 表存 `_jira-raw` 即缓存）；设置页加「校验 Jira 身份」探针（一条 JQL 验证 PAT + tester 名，通过才写 `jira_tester_verified_at`）；§13 列 Jira 管理员协调项（容量评估/WAF 白名单——该实例有 WAF 拦截前科）。

**§4 验收**：两个用户同产品并发生成互不阻塞；连点防抖提示；**kill -9 worker → 10 分钟内清扫任务把僵死 run 标 failed 且锁释放，重跑命中 checkpoint 不重付已完成步骤 LLM 费用（断言 LLM 调用计数）**；取消 5 秒内终止子进程；子进程 env 快照断言不含任何系统机密；50 模拟用户并发投递的排队时间符合 SLO。

---

## 5. 钉钉登录与权限安全

### 5.1 双路径登录（端点经官方文档逐字核实，更新至 2026-05）

**A. 浏览器扫码（端外）**：`login.dingtalk.com/oauth2/auth`（`client_id=AppKey&scope=openid corpid&prompt=consent&state=随机串`）→ 回调 ⚠️ `code` 与 `authCode` 双参数同值（都接）→ `POST api.dingtalk.com/v1.0/oauth2/userAccessToken` → 校验 `corpId`==本企业 → `GET /v1.0/contact/users/me`（header `x-acs-dingtalk-access-token`，⚠️ 返回 unionId/nick/avatar，**无 userid 无姓名**）→ `topapi/user/getbyunionid`（⚠️ 须**企业** access_token；errcode **60121=非本企业员工→拒绝**，天然准入）。

**B. 钉钉端内免登**（UA 含 `DingTalk` 走此路）：前端 `dd.requestAuthCode({corpId, clientId})` → code（⚠️ 5 分钟、一次性）→ 服务端企业 token（`/v1.0/oauth2/accessToken`，缓存 TTL 7000s）→ `topapi/v2/user/getuserinfo` → **直接得 userid+姓名**。

两路径落同一行 users（主锚 `dingtalk_userid`，辅锚 unionid）；首登自动建号（role=qa）；⚠️ `jira_tester_name` **无回退、必填+探针校验**后才放开选单/生成（v1.0 的「空=用 display_name」已删——那会让钉钉姓名进 JQL 静默 0 单）。

### 5.2 控制台配置清单

1. 创建企业内部应用：记录 Client ID/Secret/**AgentId**（Secret 进 web 容器专属 env，不进 Git）。
2. 安全设置：重定向 URL = `https://qa.<内网域名>/auth/dingtalk/callback`（⚠️ 官方教程确认 localhost/内网/http 可用——跳转发生在员工浏览器侧）；**服务器出口 IP** 填宿主机 NAT 出口（⚠️ 仅 IPv4、≤20 条；漏配报 60020；服务器需能出网访问 api/oapi.dingtalk.com:443）。
3. 网页应用能力：首页 = `https://qa.<域名>/?from=dingtalk&corpid=$CORPID$`。
4. 权限（管理员审批）：通讯录个人信息读、成员信息读、可选个人手机号。
5. **发布应用**+可见范围=QA 部门（⚠️ 不发布免登不生效）。

### 5.3 会话与 RBAC

- sessions 表 + 不透明 token cookie（HttpOnly/Secure/SameSite=Lax，12h）；登出/踢人 = revoked_at。
- `require_role("admin")` 挂 admin 路由（KB 编辑/容器树/用户管理/产品配置）；`/register` 删除。
- ⚠️ **owner-scoped 是全局规则不是单点**（v1.0 只写了 job_logs，审查升格）：仓储层只提供带 `owner_user_id=current_user`（admin 旁路）的查询入口——tickets/artifacts/runs/job_logs/selections 的读、写、**取消、删除**全部过它；路由禁止裸 `(product, key)` 查询。现状的隔离靠文件系统 contextvar，切 DB 后该隐式保护消失，必须显式重建。
- admin 兜底口令账号：隐藏入口 + 强口令 + ⚠️ **指数退避、永不硬锁账号**（只锁 IP 维度）——否则钉钉故障期攻击者持续输错口令即可锁死唯一破窗通道；兜底登录失败发钉钉告警。

### 5.4 凭证加密

Fernet 加密入 `user_credentials`；主密钥 `QA_SECRET_KEY` 经 **compose secrets/容器专属 env 文件**注入（⚠️ v1.0「.env（Docker secret）」表述有误——env_file 不是 secret 机制，按 §10.1 拆分）；MultiFernet 轮换 + key_version；只在调第三方 API 瞬间解密；不进日志不回显；⚠️ 不用 pgcrypto。存量明文凭证入库后通知全员轮换。

### 5.5 上线安全门槛清单（全绿才推广）

| # | 项 | 改造 |
|---|---|---|
| S1 | 明文凭证落盘/回显/明文端点 | Fernet 入库；删 `/settings/secret/{kind}`；UI 只掩码 |
| S2 | 开放注册 | 删 /register，钉钉建号 |
| S3 | 无 RBAC | role + require_role + admin 收口 |
| S4 | 无 CSRF | ⚠️ **双通道**（审查确认登录/设置/PAT 保存/raw 编辑器是普通表单，`hx-headers` 不生效）：HTMX 走 `X-CSRF-Token` 头 + 普通表单加 hidden `csrf_token`，中间件二者择一验证（token 绑定 session）；验收显式覆盖登录/设置/raw 编辑三类表单 |
| S5 | 无限速/无吊销 | ⚠️ uvicorn 加 `--proxy-headers --forwarded-allow-ips=<caddy 网段>`（否则全部客户端 IP=Caddy IP，IP 限速退化为全站单桶=自助 DoS，sessions.ip/审计也失真）；限速 IP 维度+指数退避；sessions.revoked_at |
| S6 | 越权读取 | 升格为 §5.3 owner-scoped 全局规则（含取消/删除） |
| S7 | audit 不可查 | audit_events + 管理页 |
| S8 | CDN 依赖 | htmx/字体全本地化进镜像 |
| S9 | 密钥单机文件 | compose secrets / 按服务 env（§10.1） |
| S10 | gitignore 未锚定 | §2.4 R1 |
| S11 | 作业日志泄密 | job_logs/argv_display 写入前脱敏（§4.2） |

**§5 验收**：双路径登录落同一账号；60121 拒绝路径有测试；A 对 B 资源全 403；S1–S11 全绿；CSRF 三类表单用例；兜底账号在持续错误口令攻击下仍可从白名单 IP 登录；OWASP ASVS L1 自查归档。

---

## 6. 产品通用化（去 WMS 化）

### 6.1 per-product 配置 schema

落 `products.config` jsonb（管理后台编辑，YAML 种子）。三层合并：代码默认 < 全局段 < products.<p> < 运行时 env/CLI，由 `core/productcfg.py` 统一提供（含 `ticket_key_regex` 编译缓存），全仓禁止直读 `cfg["selection"]`。

```yaml
products:
  wms:
    display_name: "WMS"
    jira:
      project_keys: ["EAR"]
      board_id: 236
      api_flavor: "server"                  # server|cloud（greenhopper vs /rest/agile/1.0）
      ticket_key_regex: "(?:EAR)-\\d+"      # 默认由 project_keys 派生
      fields: { tester: "customfield_10020", sprint: "customfield_10125", tester_jql: "cf[10020]" }
      sprint_name_date_regex: "(\\d{4}-\\d{2}-\\d{2})"
      resolved_resolutions: ["已修复","Fixed","Done"]
      noise_issuetypes: ["Activity","Epic"]
      noise_summary_words: ["预排期","需求复核","版本复核","复核","需求清单"]
    selection:
      tester: ""                            # 空=取登录用户 jira_tester_name（必填闸门见 §5.1）
      issuetypes: ["提高"]
      resolutions_allowed: ["已修复","Fixed"]
      split: { enabled: true, regex: "^(?P<base>.+)--(?P<suffix>[^-]{1,12})$",
               pick_priority: ["web","app","接口","pc","小程序","h5","pos"], link_types: [] }
    platforms: { source: "modules" }        # 从 modules 树派生，废除三处硬编码平台表
    output: { format: "codearts", modules_tree_heading: "## 2" }
    confluence: { spaces: [] }
    prompts:
      entry_path_style: "wms-{页面}"
      ticket_key_example: "EAR-246155"
      artifact_heading_blacklist: ["仓配","分拣","入库","出库","WMS","APP"]
default_product: ""                         # 空=禁止隐式默认
```

### 6.2 硬编码替换清单（审计 + 审查补充）

**P0（第二产品直接不工作/静默错误）**：

1. `EAR-*` glob 4 处 → `*-*` glob + regex 过滤：`run_sprint.py:45`、`webapp/services/tickets.py:75`、`select_sprint.py:108,314`（⚠️ :314 失效=重复生成已覆盖特性）；
2. `EAR-\d+` 正则 7 处 → 产品 regex：`kb_store.py:407`、`strong/revise.py:37,40`、`spot_check.py:26,280`、`qa-spot-check.js:67`、`qa_pipeline.py:136`；
3. key 形状 `[A-Z]+-\d+` **4 处**（审查补充第 4 处）：`validate-test-design.py:23`、`check-ticket-artifacts.py:23`、`webapp/services/tickets.py:16`、**`webapp/services/digest.py:25`**；替换落地后全仓再 grep `[A-Z]+-\d` / `EAR-` 作为 §6 验收步骤；
4. 平台前缀三处硬编码 → modules 树派生：`validate-test-design.py:37-39`、`digest.py:17-20`、（`validate-containers.py:45` 已会解析，作为派生源）；
5. 选单口径全局单产品 → `products.<p>.selection`：`select_sprint.py:72-84` + `:238` 双处兜底合一。

**P1**：CLAUDE.md 整文件进弱模型 system（`qa_pipeline.py:512`、`batch_generate.py:81`）→ §6.3 拆分；`default="wms"` 11 处必填化；`webapp/config.py:69,72-79` 配置化；`deps.py:108` 改 `jira_tester_name`；prompts/commands 示例模板化。

### 6.3 CLAUDE.md / 提示词拆分

通用契约留 CLAUDE.md；实例参数（§4.5 选单规则、customfield 字段号、拆单约定、账套示例）抽到 `products.config.prompts` + KB；system 拼装点改为「通用契约 + 按产品渲染附录」；slash 命令文档同步。

### 6.4 新产品脚手架与真实成本

- `python -m core.productcfg scaffold <key>`：建 products 行 + upsert kb_products + 生成 `_kb/projects/<key>/` 四件套骨架（⚠️ modules.md 必含 `## 2.` + ```text 树块，三处代码锚定）+ 待办清单输出。
- ⚠️ **诚实成本核算**（审查指出 v1.0 回避）：引擎接入≈0 代码，但**KB 内容建设是数周级人力**（WMS 的 rules/modules/黄金范例 curate 了数周，tools/kb-bootstrap 即其遗迹）。第二条真实产品线按 **2–4 周 KB curation** 估算，§11 单列交付项（含负责人、数据来源：tools/kb-bootstrap 批量拉取 + 人工提炼 + 黄金范例评审）。

**§6 验收**：① 虚构产品（不同 key、无拆单、output=none）全链路；② ⚠️ **一个 output=codearts 的产品带真实 modules 树通过 validate-containers**（可用 WMS 克隆改名作准生产演练）；③ WMS 回归无差异；④ system prompt 快照断言无跨产品污染；⑤ 替换后全仓 grep 复扫为零残留。

---

## 7. 流水线通用引擎

### 7.1 抽象

```python
class Step(Protocol):
    name: str
    def run(self, ctx: RunContext) -> StepResult: ...

class ValidatorGate(Protocol):     # = 现有校验器行协议（exit 0/1/2 + FAIL:/WARN: 行）原样保留
    def check(self, workdir: Path) -> GateResult: ...

PIPELINES = {
  # ⚠️ 顺序按真实「草稿先行·只答一次」流程（v1.0 把 HUMAN_GATE 放错在 Points 前，已修）：
  "qa_generate": [Fetch, Requirement, JiraSearch, Context, LinkedIssues, Analyze,
                  Questions, Points, Draft,
                  StrongReview,          # resolve + draft_review，折问进 questions.md
                  HUMAN_GATE,            # awaiting_input：人工只答一次
                  Design, Validate, Packet,
                  StrongFinalize],       # repair + spot_check
  "api_testgen": [SpecSnapshot, CaseSkeleton, CaseDetail, ApiValidate, EmitPytest],   # P5
}
```

- 闸门重生成环（`batch_generate.build_ticket` 的回喂/有界轮次/rc==2 短路）抽为引擎级 `gated_generate(step, gates, rounds)`——Apifox 轨只换 gates。
- MD_STEPS 五元组保留为声明式步骤定义；每步完成写 checkpoint；续跑= checkpoint+产物双判（沿用 skip-if-exists 语义）。

### 7.2 执行模型分两阶段（v1.1 重写——消除 P2↔P3 环形依赖 + relative_to 崩溃）

**阶段 A（P2，文件模式）**：worker 容器与 web 容器**共挂 userdata 命名卷**（compose 同路径挂载），worker 任务直接设 `QA_TICKETS_ROOT=<挂载点>/<user>/tickets` 跑现有 `run_sprint.py` 子进程——零物化、零回灌，PG 里只有 runs/steps/logs。此阶段产物真源仍是文件（与现状一致），webapp 读文件逻辑不动。

**阶段 B（P3a，DB 真源 + 物化）**：

```
1. 按 run 从 PG 导出涉及的工单树到 <repo>/.work/<run_id>/tickets/…（字节级还原，含 .sprint-state 兼容文件）
   ⚠️ 必须在仓库根之下 —— run_sprint.py:138,248,262 的 relative_to(REPO_ROOT) 约束；
      同时给这三处打 relpath 容错补丁 + 「QA_TICKETS_ROOT 在仓库外」回归测试（双保险）
2. 子进程白名单 env（§4.2）跑管线
3. ⚠️ 增量回灌：每完成一个工单即回灌该单产物（rev+1、刷新派生列、留 revision、写 checkpoint）
   —— 不是 run 结束一次性回灌（kill -9 不丢中间产物、不重付 LLM 费用）
4. .work/<run_id>/ 在命名卷上，重试按 run_id 复用；run 成功后清理
5. 回灌白名单 = 已知产物名集合；未知文件告警不入库
```

- Claude Code 交互轨：`/qa:*` 前 `export`、后 `ingest`（`qa sync` 命令）——CLI 体验不变。
- 终态：webapp 读写全走 artifact store API（rev 乐观锁）；管线执行长期可停留在物化方案，待 core/ 包化后再评估直读 DB。

### 7.3 沿用机制（不动）

questions.md 三形态契约与外科手术写回、normalize+validate 双闸门、黄金范例、强模型五维对抗复核、「LLM proposes / deterministic Python disposes」。

**§7 验收**：同一工单文件模式 vs 物化模式产物逐字节一致；`QA_TICKETS_ROOT` 指仓库外 tmp 的回归测试通过；kill worker 后续跑 checkpoint 命中（LLM 调用计数断言）；api_testgen 仅靠注册表+新 gates 挂载成功（P5 实证）。

---

## 8. Apifox 接口测试（两轨）

> ⚠️ 关键事实（官方文档核实）：Apifox 开放 API 只有 3 个端点（导入 OpenAPI/导入 Postman/导出 OpenAPI），**不能编程写测试用例/读结果**——「写回 Apifox」不存在。

### 8.1 轨道一：Apifox 平台内（零工程量，可与 P1 并行推广）

管理员开 AI（BYOK，配公司百炼 key，客户端 ≥2.7.37）→ QA 用 AI 生成用例 + 测试场景编排 → `apifox-cli` 进 Jenkins/GitLab CI（`apifox run --access-token … -t <场景id> -r html,junit --upload-report`）→ 失败通知钉钉群。

### 8.2 轨道二：平台内生成 pytest 套件（P5）

```
触发：⚠️ 默认只用 maintenance 定时轮询 export-openapi + version_hash 比对
（v1.0 的 Apifox webhook 已砍：webhook 需要公网【入站】，与「唯一公网依赖=出站」的内网架构矛盾；
 若将来确需准实时，单独设计带 IP 白名单+签名+防重放的隔离入口，且先实证 Apifox 签名机制）
  POST https://api.apifox.com/v1/projects/{id}/export-openapi
  Header: Authorization: Bearer <服务 token>, X-Apifox-Api-Version: 2024-03-28
  → api_specs 快照（spec 原文 text + hash + diff 摘要）→ 变更钉钉提醒负责人
生成：api_testgen 管线（§7.1）= 弱模型骨架/补全 ⇄ ApiValidate 闸门（OpenAPI 引用一致性/断言完整性）
      → 强模型对抗复核（维度：参数边界/鉴权矩阵/幂等/脏数据回滚）→ EmitPytest（pytest+httpx，参数化+响应 schema 校验）
执行：worker 子进程 pytest → JUnit → api_test_runs 入库 → HTMX 看板
旁路：schemathesis v4 对同一 spec 做属性模糊测试（免 LLM）
```

**环境模型（v1.1 新增——审查指出执行侧只设计了一半）**：

```sql
CREATE TABLE api_environments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product text NOT NULL REFERENCES products(key),
  name text NOT NULL,                  -- 'api-test' / 'dev'
  base_url text NOT NULL,
  auth_kind text NOT NULL DEFAULT '',  -- token|basic|login-flow
  auth_ciphertext text NOT NULL DEFAULT '',   -- Fernet（环境级凭证）
  data_policy text NOT NULL DEFAULT '',       -- 数据准备/清理约定说明
  UNIQUE (product, name)
);
```

⚠️ **爆炸半径用机制不靠纪律**：pytest 执行器与 schemathesis **强制 base-URL 白名单**——目标 host 必须命中 api_environments 登记的测试环境，否则拒跑并记审计；EmitPytest 渲染时强制覆写 spec 的 `servers` 段；schemathesis 跑在独立 test-runner 容器，compose 网络策略限制其可达地址（不给它 Jira/Confluence/生产网段路由）。**专用 API 测试环境的存在与归属是 P5 前置依赖**（§13 决策项）。

- Apifox 服务 token：专用机器人账号申请（全权且只显示一次）→ ⚠️ 走 env/secrets，**不入 user_credentials**（CHECK 约束与行级语义不容纳系统凭证）。
- Dredd 已归档不引入；开源 LLM 测试生成工具无 Python 原生输出，自建复用现有管线。

**§8 验收**：spec 轮询发现变更并提醒；5 个真实接口生成套件人工评审达草稿水准；白名单拦截测试（伪造 spec servers 指向非白名单 host → 拒跑+审计）；schemathesis 在专用环境跑通出报告；轨道一 CI 跑通一个场景集。

---

## 9. 前端抛光

Tailwind + DaisyUI（CSS-only，hx-swap 无重初始化问题）+ Alpine.js；`docs/DESIGN.md` 令牌映射 Tailwind theme（完成后 DESIGN.md 归档 docs/history/）；静态资源全本地化（S8）；逐页：登录→看板→工单详情→设置/管理。逃生舱：复杂页面（Apifox 用例编辑器）做 Vue 3 + Element Plus 岛（⚠️ Ant Design Vue 已停更 2024-11，不选）；htmx 锁 2.x。

---

## 10. 部署与运维

### 10.1 compose 拓扑（v1.1：migrate 独立、secrets 按服务拆分、共享卷）

```yaml
services:
  caddy:
    image: caddy:2
    ports: ["443:443","80:80"]
    volumes: [./Caddyfile:/etc/caddy/Caddyfile, caddy_data:/data]
  migrate:                                   # ⚠️ 一次性迁移服务（修首次部署竞态）
    build: { context: .., dockerfile: deploy/Dockerfile }
    command: sh -c "alembic upgrade head && procrastinate schema --apply"
    env_file: env/web.env
    depends_on: { db: { condition: service_healthy } }
    restart: "no"
  web:
    build: …same image…
    command: uvicorn webapp.main:app --host 0.0.0.0 --port 8800 --workers 4
             --proxy-headers --forwarded-allow-ips=<caddy 网段>     # ⚠️ S5 依赖真实客户端 IP
    env_file: env/web.env                    # DB+SECRET_KEY+DINGTALK_*（无 APIFOX_TOKEN）
    volumes: [userdata:/app/userdata, work:/app/.work]
    depends_on: { migrate: { condition: service_completed_successfully } }
    healthcheck: { test: ["CMD","python","-c","import urllib.request;urllib.request.urlopen('http://localhost:8800/healthz')"] }
  worker-gen:
    build: …same…
    command: procrastinate --app=worker.app worker --concurrency=3 --queues=generation --name=gen-1
    env_file: env/worker-gen.env             # DB+SECRET_KEY（⚠️ 无钉钉 Secret、无 Apifox）
    volumes: [userdata:/app/userdata, work:/app/.work]
    depends_on: { migrate: { condition: service_completed_successfully } }
  worker-review:
    build: …same…
    command: procrastinate --app=worker.app worker --concurrency=6 --queues=review,maintenance
    env_file: env/worker-review.env          # DB+SECRET_KEY+钉钉通知+APIFOX_TOKEN
    volumes: [userdata:/app/userdata, work:/app/.work]
    depends_on: { migrate: { condition: service_completed_successfully } }
  db:
    image: pgvector/pgvector:pg16
    volumes: [pgdata:/var/lib/postgresql/data]
    environment: { POSTGRES_DB: qa_platform, POSTGRES_USER: qa, POSTGRES_PASSWORD_FILE: /run/secrets/pg_pw }
    secrets: [pg_pw]
    healthcheck: { test: ["CMD-SHELL","pg_isready -U qa"] }
  backup:
    image: prodrigestivill/postgres-backup-local
    environment: { SCHEDULE: "@daily", … }
    # ⚠️ 备份出口码接钉钉告警（静默失败数周=真实的全量丢失故事）；每周恢复演练留痕
secrets:
  pg_pw: { file: ./secrets/pg_pw }           # ⚠️ 顶层 secrets 定义（v1.0 缺失）；DB 密码不再在 env 明文复现
volumes: { pgdata: {}, userdata: {}, work: {}, caddy_data: {} }
```

要点：单镜像多角色；env 按服务拆分（任一容器沦陷不等于全平台凭证沦陷）；DB 按角色建最小权限账号（web/worker 用业务账号，migrate 用 owner）；test-runner（P5）单独服务+受限网络。

### 10.2 运维基线

| 项 | 方案 |
|---|---|
| 备份 | 每日 pg_dump + **失败告警（钉钉机器人）** + 每周恢复演练（restore 临时库跑 smoke）留痕 |
| 日志 | JSON stdout；audit_events 在库 |
| 监控 | /healthz + `procrastinate healthchecks` + 队列深度/排队时长告警 + PG 连接数告警 |
| 升级 | 镜像 tag；migrate 服务前置；回滚=上一 tag + alembic downgrade（迁移必写 down） |
| 出网 | 仅出站：模型端点、Jira/Confluence、api/oapi.dingtalk.com、api.apifox.com；**无入站公网** |
| Jira | worker 层全局令牌桶限速 + issue 快照缓存；与 Jira 管理员对齐容量/WAF 白名单 |
| Windows 开发机 | web 本机跑 + PG/worker 走 Docker Desktop/WSL2（与生产同构） |

**§10 验收**：首次部署一条 `docker compose up -d` 成功（含迁移顺序）；压测报告（含 50 并发 SSE 场景）；备份-恢复演练记录；拔掉 worker 容器的故障演练记录。

---

## 11. 分阶段实施计划（v1.1 重排）

> 1 名开发 + AI 结对；估时含 30% buffer；**每阶段结束=可运行可回滚**。

| 阶段 | 内容 | 工期 | 出口条件（验收摘要） |
|---|---|---|---|
| **P0 准备** | §2.4 R1 清理；deploy/ 骨架；Alembic 基线；**钉钉应用创建/权限申请（审批有等待期，第一件事）**；百炼配额核实 + 容量模型初版（§4.6） | 1 周 | 测试全绿；dev compose 可起；钉钉沙箱可用；容量模型评审过 |
| **P1 安全与身份** | users/sessions/credentials/products/audit 五表；钉钉双路径；RBAC；Fernet；S1–S11；jira_tester_name 探针 | 2.5 周 | §5 验收全过；users.json 迁移+轮换通知 |
| **P2 作业队列（文件模式）** | Procrastinate + runs/steps/logs 表；**worker 共享 userdata 卷文件模式直跑**；僵死清扫；白名单凭证注入；SSE(async)/轮询；钉钉通知；JobManager 退役；**用户操作指南+答题流程培训材料** | 2.5 周 | §4 验收全过；⚠️ **培训材料是出口条件**（试点前必备，不是 P4 才做） |
| **试点** | 10–20 人（含 2–3 个非开发背景 QA） | 2 周（与 P3a 并行） | SLO 达成；反馈清单收敛 |
| **P3a 存储入库** | tickets/artifacts/ledger/selections 表；按用户割接程序+演练；webapp 读写切 DB（owner-scoped + rev 锁）；物化执行器（含 relative_to 补丁+增量回灌+持久 .work 卷）；coverage 事务认领；kb_store 三修 | 3 周 | §3/§7 验收全过；割接演练 ≤15 分钟/用户 |
| **P3b 产品通用化** | productcfg 三层合并；P0/P1 硬编码替换（~30 处）；CLAUDE.md 拆分；scaffold；grep 复扫 | 2 周 | §6 验收①③④⑤ |
| **P4 部署与推广** | 生产 compose；压测（Locust 含 SSE 场景）；备份/故障演练；50 人 → 全量灰度 | 1 周 + 灰度 2 周 | §10 验收；事故预案演练 |
| **P5 Apifox 两轨** | 轨道一推广（可提前并行）；spec 轮询 + api_testgen + pytest 执行器 + 白名单机制 + schemathesis + 环境模型 + 看板 | 4 周 | §8 验收（⚠️ 前置：专用测试环境到位） |
| **P6 第二真实产品** | scaffold + **KB curation（tools/kb-bootstrap 拉取→人工提炼→黄金范例）** + §6 验收② 准生产演练 | 2–4 周（人力主要在 KB） | 真实产品全链路 + validate-containers 通过 |
| **P7 前端抛光** | Tailwind+DaisyUI 逐页（可与 P3–P5 穿插） | 1–2 周 | 设计走查 |

总计约 **16–20 周**到全量（v1.0 的 12–15 周偏乐观，审查后加 buffer 与缺项）。关键路径：钉钉审批（P0 即办）→ P1 → P2 → 试点；P3a/P3b 在试点反馈期推进。

---

## 12. 风险清单与回滚

| 风险 | 概率/影响 | 缓解 |
|---|---|---|
| ⚠️ 百炼共享 key 配额 < 周三峰值需求（**最大单一风险**） | 中/高 | P0 核实配额建容量模型；SLO+排队深度告警；错峰定时投递；必要时按队列深度扩 worker 或推自配 key |
| 字节契约被破坏 | 中/高 | 导入/物化双向 sha256；真实历史单逐字节回归 |
| 物化/回灌边界 bug | 中/中 | 产物名白名单；增量回灌+revision 可整体还原；文件模式是常驻退路 |
| 僵死作业/锁泄漏 | 中/中 | retry_stalled_jobs 清扫 + run 对账 + 故障演练进验收 |
| Jira Server 负载/WAF | 中/中 | 全局限速+快照缓存+管理员协调（§13） |
| 钉钉审批/出口 IP 受阻 | 中/中 | P0 即办；admin 兜底（防自锁设计）；60020 预案 |
| Procrastinate 维护风险 | 低/中 | MIT 可 fork；语义都在 PG 表，可换实现 |
| 多用户割接出错 | 低/高 | 按用户割接+演练+tar 快照+导出回滚命令 |
| 单 PG 实例 | 低/高 | 每日备份+失败告警+每周恢复演练；接受 RPO=24h |
| 单人排期超支 | 中/中 | P3 已拆 a/b；每阶段独立可上线，砍尾不砍腰 |

**总回滚策略**：每阶段 tag + DB dump；P3a 前文件是真源（回滚=旧镜像）；P3a 后回滚=导出回文件树 + 旧镜像。

---

## 13. 待你拍板的事项（v1.1 扩充）

1. **覆盖账本全局化**：建议产品全局去重（事务认领，§3.2）；保留各管各的则 PK 加 owner——确认哪种？
2. **历史产物迁移范围**：建议全量 6 个 sprint 入库（仅 22MB）。
3. **钉钉应用**：哪位管理员配合建应用/批权限/发布——P0 第一件事。
4. **生产机规格**：建议 8C16G + 200G SSD（compose 单机含 PG + 双 worker）；最低 4C8G。
5. **Apifox 服务账号**：新建机器人账号出 Access Token（不用个人号）。
6. **百炼配额**（新增，最大风险项）：确认团队 key 的 QPM/TPM/日额度，容量模型要用。
7. **专用 API 测试环境**（新增，P5 前置）：是否存在/谁提供 base_url 与测试凭证、数据准备策略。
8. **Jira 管理员协调**（新增）：几百人 PAT 接入 + 请求量放大的容量评估与 WAF 白名单。
