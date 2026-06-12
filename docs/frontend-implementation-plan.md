# qa-workflow v3 前端 · 详细实施方案

> 本文档基于对现有代码的逐文件研究 + 一手官方文档核实编写，供团队照此编码。
> 选定前提（用户已拍板）：**① 小团队多人使用 ② 前端用 HTMX + FastAPI ③ 强模型审计步骤现在就用 Claude Agent SDK 无头移植**。
> 关联背景：根级 `README.md` 路线图 v3、`CLAUDE.md`、`docs/`（本文件）。

---

## 0. TL;DR（一页看懂）

- **拓扑**：单个共享后端实例（FastAPI）独占仓库工作树作为数据源；多名成员用浏览器访问，走 App 级登录鉴权。**不引入数据库**——`tickets/` + `.sprint-state/` 平铺文件仍是唯一真源。
- **后端三类调用**：① 瞬时只读（选单/校验/读产物）= **直接 import** 现有脚本；② 分钟级弱模型批量生成 = **子进程跑现成 `run_sprint.py` CLI**（隔离 `SystemExit`、复用全部续跑/选单逻辑）；③ 强模型审计 = **Agent SDK(Python) 异步任务**（移植两个 `.js`）。
- **前端**：HTMX + Jinja2 + FastAPI 服务端渲染；只读树视图用一小块原生 JS（或 NiceGUI 子页）。**不引入 React/Vite/Tailwind 构建链**。
- **多人新增项（关键）**：登录鉴权、**工单级写锁 + 乐观并发(mtime/hash)**、操作者身份留痕、绑定内网+TLS+访问控制、**单产品作业串行锁**。**多数情况下不需要按用户注入凭证**（用一套只读服务账号即可，详见 §3.2）。
- **强模型移植要点**：`agent()→query()` 结构化输出、`parallel/pipeline→asyncio`、镜像 `.claude/settings.json` 的工具白/黑名单、**算术维度用「数值 AST 求值器」工具取代裸 Bash**（多人+客户数据环境下裸 Bash 是 RCE/泄露隐患）、上线前做 **shadow-run 平价评审**。注意 **2026-06-15 起 Agent SDK 走独立月度额度池**。

---

## 1. 现状与边界（为什么是这套方案）

### 1.1 引擎边界——整个方案的根因

后端被一条**引擎边界**劈成两半，前端调 AI 的路径完全由它决定：

| 层 | 现状 | 可否服务端无头 | 工作量 |
|---|---|---|---|
| **弱模型全链**（`fetch→…→design→validate→packet`） | 纯 Python，`qa_pipeline.run_pipeline()` / `run_sprint.py` / `batch_generate.build_ticket()` 全可 import | ✅ 已经是 | 包 API：**低** |
| **确定性校验器**（5 个） | 纯 stdlib、离线、零成本 | ✅ | 低（部分可 import、部分 subprocess） |
| **只读 Jira / 选单** | `jira_fetch.*`（urllib）、`select_sprint.plan()` | ✅ | 低 |
| **强模型审计**（`/qa:resolve`、`/qa:spot-check`） | `.claude/workflows/*.js`，依赖 Claude Code 注入的 `agent/pipeline/parallel/log/args`，**无 CLI、无 HTTP** | ❌ 必须用 Agent SDK 重写 | **中** |

> 已核实（官方 `code.claude.com/docs/en/agent-sdk`）：Agent SDK 与 Claude Code 同一 agent loop，Python+TS 双版本，无头运行，**自动加载工作目录的 `.claude/commands/*.md` + `CLAUDE.md` + MCP 服务器**，支持子 agent、流式输出、结构化输出、hooks、权限白/黑名单。→ 现有 `CLAUDE.md`、`.claude/commands/qa/*.md`、Atlassian MCP **可被无头复用**；**只有两个 `.js` 需要重写**。

### 1.2 钉死技术选型的硬事实

1. **状态全是平铺文件、无 DB**：前端即读写 `tickets/<product>/<sprint>/<EAR>/` 与 `tickets/<product>/.sprint-state/`。**别加 DB**（会和文件真源打架）。
2. **`test-design.json` 要整段粘进 CodeArts** → **字节级一致不可破**（规范序列化 = `json.dumps(data, ensure_ascii=False, indent=2)+"\n"`，见 `batch_generate.py:300`）。这恰是"做结构化拖拽树编辑器"风险最大处（序列化漂移会悄悄毁掉粘贴），而 **CodeArts 自身就是那棵树的编辑器** → v3 **只做只读树视图 + 校验标记 + raw 文本编辑**，结构化编辑交还 CodeArts。
3. **唯一人工闸门 = `questions.md` 答题**（`qa_pipeline.py:534-553` 的 `ensure_questions_format()` 硬阻断）→ **这才是核心交互页**。
4. **`SystemExit` 当错误类型**：`run_pipeline` 在 `qa_pipeline.py:574`、`jira_fetch`/`_load_env` 多处 `raise SystemExit`。→ 任何 in-process 调用**必须 `try/except (SystemExit, RuntimeError)`**，否则拖垮 worker。
5. **密钥只在 gitignored `config/config.local.yaml`**，绝不能进浏览器；`tickets/` 含客户账套号（`rules.md §0.16`）→ **访问控制是硬需求**。

---

## 2. 总体架构

```
                         ┌─────────────────────────────────────────────┐
   浏览器(多名成员)        │            FastAPI 单实例（内网 + TLS）          │
   HTMX + Jinja 片段  ←→  │  Auth/Session ─ 路由 ─ Job Manager            │
        │  SSE 进度        │     │            │             │              │
        │                 │     │(瞬时)       │(分钟级)      │(强模型)       │
        │                 │  import 调用   subprocess     Agent SDK(异步)   │
        │                 │   ·校验器       run_sprint.py   ·spot-check 端口 │
        │                 │   ·select_sprint  (CLI)        ·resolve 端口     │
        │                 │   ·读 md/json   ·弱模型批量     ·算术 AST 工具    │
        │                 │     │            │             │  + Atlassian MCP│
        │                 └─────┼────────────┼─────────────┼──────────────┘
        │                       ▼            ▼             ▼
        │              ┌───────────────────────────────────────────┐
        └─────────────►│  仓库工作树（唯一真源，单实例独占 + 写锁）        │
                       │  tickets/<product>/<sprint>/<EAR>/*.{md,json} │
                       │  tickets/<product>/.sprint-state/*.{json,md}  │
                       │  _kb/  config/config.local.yaml(仅服务端读)    │
                       └───────────────────────────────────────────┘
```

**关键原则**：**单实例独占一份工作树**。多人 = 多浏览器客户端，不是多实例。所有写经过该实例的锁串行化，工作树始终一致；git 提交对工作树做快照（可做成一个"提交&推送"按钮，或继续手动）。**严禁**让每个成员各跑一个实例对各自 clone（必然状态分裂、合并地狱）。

---

## 3. 后端设计

### 3.1 进程 / 作业模型

| 调用类别 | 实现方式 | 理由 |
|---|---|---|
| 只读瞬时（选单表、校验、读产物、配置读） | **in-process import** | `select_sprint.plan()` 零 LLM 瞬时；校验器毫秒级 |
| 弱模型批量生成（`run_sprint`/单工单链） | **`subprocess.Popen(argv 列表)` 跑 `run_sprint.py`** | 隔离 `SystemExit`、复用 `--select/--until/--resume-after-questions/--keys` 全部逻辑、天然不阻塞事件循环 |
| 强模型审计（resolve/spot-check） | **Agent SDK 异步任务**（FastAPI 同进程 asyncio，或独立 worker） | SDK `query()` 本就是 async，与 FastAPI 自然契合；流式 → SSE |

- **Job Manager**：内存作业表 `{job_id: {type, product, sprint, status, started, log[], rc}}`；每个 job 一个后台任务（`asyncio.create_task` 或 `ProcessPool`）。
- **单产品作业串行锁**：同一 `product` 同时只允许一个生成/审计 job（否则两人同时批量会争抢同一批文件）。用 `asyncio.Lock` per-product 或一张"运行中"表 + 入队。
- **子进程参数安全**：工单号/日期一律以 **argv 列表**传入 `Popen`，**绝不 `shell=True`**（即便可信用户也防 fat-finger 注入）。

```python
# 弱链批量：子进程驱动现成 CLI，逐行读 stdout 推 SSE
async def run_weak_batch(product, sprint, extra_args, on_line):
    args = [sys.executable, "scripts/run_sprint.py", "--product", product,
            "--sprint", sprint, "--select", *extra_args]   # 列表，非 shell
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=REPO_ROOT, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    async for raw in proc.stdout:
        await on_line(raw.decode("utf-8", "replace").rstrip())   # → SSE
    return await proc.wait()
```

### 3.2 多人改造清单（重点，按必要性排序）

| # | 项 | 必要性 | 做法 |
|---|---|---|---|
| 1 | **登录鉴权** | 必须 | 小团队：一张用户表 + session cookie（如 `fastapi-users` 或自写 + `itsdangerous` 签名 cookie）；若公司有 SSO/LDAP 则接入。后端绑内网 + 反向代理 TLS。 |
| 2 | **工单级写锁 + 乐观并发** | 必须 | 写 `questions.md` / `test-design.json` 前：①取 per-ticket `asyncio.Lock`；②**乐观校验**：前端提交带上读时的 `mtime`(或内容 hash)，不匹配则 409 让用户刷新——防"A 编辑时 B 的批量 `--force` 覆盖"。 |
| 3 | **操作者身份留痕** | 必须 | 谁答了哪题、谁触发了哪次审计，写入审计日志 / `_resolve.md` 备注。`questions.md` 人工答案区可不带身份（保持产物干净），身份留在旁路审计。 |
| 4 | **访问控制** | 必须 | `tickets/` 有客户账套号 → 登录后才可读；按需要再做产品级权限。 |
| 5 | **单产品作业串行锁** | 必须 | 见 §3.1，防并发批量争文件。 |
| 6 | **按用户注入凭证** | **通常不需要** | 详见下方说明。用**一套只读服务账号**即可，省掉最重的改造。 |

**关于凭证注入（澄清一个常见误区）**：研究里把"多租户按用户注入凭证"列为阻塞项，但**你们是小团队共享、且 Jira 是只读**——用**一套只读服务账号**（现有 `config.local.yaml` 那套）就够，`run_pipeline` 在 `qa_pipeline.py:515` 内部自读配置**完全可用、无需改**。只有当"每个成员必须用自己的 Jira 身份访问"成为硬需求时，才做下面这个**可选**小改造：

```python
# 可选：让 run_pipeline 支持注入 env（仅在需要按用户凭证时才做；否则不动）
def run_pipeline(key, product, sprint=None, only=None, redo=None,
                 rounds=3, max_tokens_json=32000, env=None):   # ← 新增 env
    import _load_env
    env = env or _load_env.parse_config()                       # ← 仅这一行改动
    ...  # 其余不变；下游 jira_fetch.get_issue(key, env)/generate(...,env=...) 已接受 env
```

> 注：弱链下游 `jira_fetch.*`、`cheap_model.generate(...)` **本就接受 `env=`**，所以注入是 2 行改动；但**没需求就别做**。

### 3.3 错误处理规约（硬性）

```python
def invoke(fn, *a, **kw):
    """所有 in-process 调用现有脚本都过这层。"""
    try:
        return {"ok": True, "data": fn(*a, **kw)}
    except SystemExit as e:          # jira_fetch/_load_env/run_pipeline 用它当错误
        return {"ok": False, "error": str(e), "kind": "system_exit"}
    except RuntimeError as e:        # ensure_questions_format 闸门失败
        return {"ok": False, "error": str(e), "kind": "gate"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "kind": "exc"}
```

### 3.4 API 端点清单（FastAPI）

> HTMX 模式下，多数端点返回 **HTML 片段**（用于 `hx-swap`），少数返回 JSON / SSE。

| Method | Path | 作用 | 底层 | 类型 |
|---|---|---|---|---|
| GET | `/` | 看板首页（产品/ sprint 选择） | 文件扫描 | HTML |
| GET | `/sprint/{product}/{date}` | sprint 看板（run/skip 决策 + 校验状态 + 进度） | `select_sprint.plan()` + 读 `.sprint-state/*` | HTML |
| POST | `/sprint/{product}/{date}/select` | 重新选单（只读 Jira，写报告） | `select_sprint.plan/write_reports` | HTML 片段 |
| POST | `/jobs/generate` | 启动弱链批量（可带 `--until questions` / `--resume-after-questions`） | 子进程 `run_sprint.py` | JSON `{job_id}` |
| GET | `/jobs/{id}/events` | **SSE** 实时进度（子进程 stdout 行 + 完成事件） | Job Manager | SSE |
| GET | `/jobs/{id}` | 作业状态轮询兜底 | Job Manager | JSON/HTML |
| GET | `/ticket/{product}/{date}/{ear}` | 工单详情（多 Tab：需求/分析/测试点/校验/树） | 读 md/json | HTML |
| GET | `/ticket/.../questions` | 答题表单（解析 3 形态） | 读 `questions.md` | HTML |
| POST | `/ticket/.../questions` | 保存答案（乐观并发 + 原子写 + 校验闸门） | `normalize_text`+`validate`+原子写 | HTML 片段 |
| GET | `/ticket/.../test-design` | 只读树 + 校验标记 + raw | `Validator(path).validate()` | HTML |
| POST | `/ticket/.../test-design/validate` | 校验 raw（不落盘预检） | `Validator` | JSON(issues) |
| POST | `/ticket/.../test-design` | 保存 raw（字节写 + 校验门） | 原子写 | HTML 片段 |
| POST | `/jobs/resolve` | 触发强模型证据消解（Agent SDK） | resolve 端口 | JSON `{job_id}` |
| POST | `/jobs/spot-check` | 触发强模型抽检（Agent SDK） | spot-check 端口 | JSON `{job_id}` |
| GET | `/ticket/.../spot-check` | 渲染 `_spot-check.md` / 结构化发现 | 读文件 | HTML |
| GET/POST | `/settings` | 配置（**密钥掩码只写**） | `_load_env` | HTML |

### 3.5 安全

- **绑定**：内网 host + 反向代理（Nginx/Caddy）做 **TLS**；不要裸 `0.0.0.0` 明文。
- **密钥**：配置页所有 `*_token` / `*api_key` 字段**掩码 + 只写**（显示 `已配置`，接受新值，绝不回显当前值）；**永远不要把 `python scripts/_load_env.py` 的原始 stdout 发给前端**（它会打印明文 token）。
- **Jira 只读双锁保持**：`.claude/settings.json` 的 deny 列表 + `READ_ONLY_MODE=true`；前端**不出现任何写回 Jira 的按钮**。
- **审计**：登录、触发生成/审计、保存答案、保存 JSON 都记审计日志（含用户、时间、工单）。

---

## 4. 强模型无头移植（Claude Agent SDK · Python）

### 4.1 原语映射

| `.claude/workflows/*.js`（Claude Code 注入） | Agent SDK(Python) 等价物 |
|---|---|
| `args.ticketDirs` / `args.product` | 函数入参 |
| `agent(prompt, {schema})` | `query(prompt, options=ClaudeAgentOptions(..., output schema))` → 取结构化输出对象 |
| `parallel(thunks)` | `await asyncio.gather(*[t() for t in thunks])`（用 `asyncio.Semaphore` 限并发 ~CPU-2） |
| `pipeline(items, s1, s2, …)` | 每个 item 独立 `await s1→s2→…`，再 `asyncio.gather` 跨 item（复刻"无栅栏、各 item 独立推进"） |
| `log(msg)` | 推到该 job 的 SSE 事件流 + `logging` |
| 子 agent 读文件/grep | `allowed_tools=["Read","Glob","Grep"]`（SDK 自动加载 `.claude` + `CLAUDE.md` + MCP） |

### 4.2 `qa-spot-check` 端口（**先做**——只读、爆炸半径最小）

源逻辑（`.claude/workflows/qa-spot-check.js`）：
- **Stage 1 Check**：5 维并行 `DIMS = [coverage, arithmetic, semantic, scope, integrity]`，每维一个 agent 读 `test-design.json + analysis/requirement/business-context/questions/test-points/linked-issues`，产 `FINDINGS_SCHEMA{findings:[{dimension,severity,test_point,problem,evidence,suggested_fix}]}`；**arithmetic 维度强制独立复算**。
- **Stage 2 Verify**：对每条发现并行对抗式核验 → `VERDICT_SCHEMA{is_real,confidence,reasoning,severity_adjusted}`；`rootTicketFalsePositive()` 过滤掉"根节点是工单号"这类误报。
- 产出每单 `{ear, dir, confirmed[], checked}`；**`_spot-check.md` 由主控写**（不是 workflow 写）。

移植要点：
1. 5 维 `parallel` → `asyncio.gather`；两阶段 `pipeline` → 每单独立链 + 跨单 `gather`。
2. `FINDINGS_SCHEMA` / `VERDICT_SCHEMA` 原样作为 SDK 结构化输出 schema。
3. **`rootTicketFalsePositive()` 正则原样保留**（移植成 Python）。
4. **算术维度：用「数值 AST 求值器」自定义工具取代裸 Bash**（见 §4.4）。
5. `_spot-check.md` 由 **Python 编排层按 `confirmed[]` 用确定模板写**（无需再起 LLM），并保留 severity 分级（🟡 等）与 JSON 节点定位。

### 4.3 `qa-resolve` 端口（**后做**——会写 `questions.md`）

源逻辑（`.claude/workflows/qa-resolve.js`）3 阶段：
- **Resolve**：每单一 agent，处理既有 `## Q` 题（试答有据者）+ 证据完整性审查补漏问 → `RESOLVE_SCHEMA{resolutions[], missing_questions[]}`。
- **Verify**：对每条"已据消解"对抗式核验 → 不支持则降级 needs_human。
- **Apply**：把答案/漏问写回 `questions.md`（**只填空、绝不碰人工已填**）+ 写 `_resolve.md` → `APPLY_SCHEMA`。

移植**铁律**（务必保留）：
- **人工已填答案为终裁**：`already_answered=true` 一律跳过、绝不修改。
- **只动 `✅ 答案` 区 / 需人工提示行 / 追加的连续 `## QN` 题块**；写回后必须仍通过 `validate-questions.py`（见 §6.3 的格式契约）。
- 漏问**绝不**来自 `test-design.json`（人工回答前不收割 JSON）。
- Apply 阶段 agent 需 `allowed_tools` 含 `Read/Edit/Write`；但**最稳的是**：让 Python 编排层用结构化结果**确定性地写** `questions.md`（按 §6.3 格式拼接），把 LLM 仅用于"判断/答"，**写盘交给代码** → 可控、可测、可回滚（写前 `.bak`，写后过 `validate-questions.py`，不过则回滚）。

### 4.4 算术沙箱——用「数值求值器」而非裸 Bash（安全关键）

源 `.js` 让 arithmetic agent **"用 Bash 跑 python 复算"**。在**多人 + 客户数据**的服务器上给 agent 裸 Bash = RCE + 数据外泄面。改为**自定义 MCP/工具**：

- **首选**：`recompute(expression)` 工具——用 Python `ast` 解析 + **数值白名单求值**（只允许 `+ - * / // % ** ( )`、`round/min/max/abs/sum/ceil/floor`、数字字面量），**禁止**名称引用/属性/调用任意函数/导入。覆盖绝大多数"个数/数量/金额/取整/分配/累计/阈值"复算，且**零代码执行风险**。
- **次选**（确需多步过程计算）：受限子进程——`python -I`（隔离模式）、`cwd=临时目录`、最小 `env`（无凭证、无网络相关变量）、`timeout`、输出大小上限、用后即删临时目录。
- 通过 SDK 的**权限白名单**只放行该工具；**绝不**放行 `Bash`/`Write` 给 spot-check 的 agent。

### 4.5 权限、MCP、凭证、额度

- **镜像 `.claude/settings.json`**：把 allow（本地读 + 全部 atlassian 读工具）/ deny（所有 atlassian 写工具）翻译成 SDK 的 `allowed_tools` + 权限回调；spot-check agent 额外**只**加 `recompute` 工具，resolve 的 apply（若走 agent 写）才加 `Edit`。
- **MCP 复用**：SDK 自动加载项目 MCP，Atlassian 只读链路直接可用；或继续用 `jira_fetch.py` 走直连（弱链已在用）。
- **凭证**：SDK 强模型需 `ANTHROPIC_API_KEY`（或 Bedrock/Vertex 等）。服务端环境变量注入，**不进浏览器**。
- **额度**：**2026-06-15 起 Agent SDK / `claude -p` 在订阅套餐下走独立月度额度池**。强模型审计**保持按需触发**（不要自动全量跑），UI 提示额度归属。

### 4.6 上线前验收：shadow-run 平价评审

LLM 输出非确定，**不能做字节 diff**。做法：选 3-5 个**已用 Claude Code 跑过 resolve/spot-check 的工单**，用 Python 端口"影子运行"，**人工对比**：confirmed 发现是否大体一致、resolve 的"自动消解 vs 留人工"分类是否一致、`questions.md` 写回是否仍过校验。通过才切流量；保留"复制命令贴回 Claude Code"作为**永久兜底开关**。

---

## 5. 前端设计（HTMX + Jinja2）

### 5.1 页面与路由

| 页面 | 路由 | 关键交互 |
|---|---|---|
| 看板首页 | `/` | 产品 + sprint 选择；近期作业状态 |
| sprint 看板 | `/sprint/{p}/{date}` | run/skip 决策表（来自 `_selection-<date>.json`）、每单 JSON校验/产物校验/测试点/待确认徽章、进度条、`生成`/`续跑`/`重选`按钮 |
| 工单详情 | `/ticket/{p}/{date}/{ear}` | Tab：需求 / 分析 / 测试点 / **答题** / **树+校验** / 抽检 / SOP 清单 |
| 答题闸门 | `…/questions` | **核心**，见 §5.3 |
| 树视图 | `…/test-design` | 只读思维导图 + 内联 FAIL/WARN + raw 编辑，见 §5.4 |
| 抽检结果 | `…/spot-check` | 渲染 `_spot-check.md` / 结构化发现，按维度+严重度分组 |
| 配置 | `/settings` | 掩码只写 |

### 5.2 sprint 看板数据来源

- 决策表：`select_sprint.plan()`（瞬时）或读 `_selection-<date>.json` 的 `decisions[]`（`role/decision/reason/platform`）。
- 进度：轮询/SSE `_sprint-progress-<date>.json` 的 `{done:[]}` ÷ `run_list`。
- 每单徽章：`validate-test-design.py` + `check-ticket-artifacts.py` 退出码 + `count_stats`（复用 `run_sprint.ticket_stats`）。
- 覆盖视图：`coverage-ledger.json`（按 `source=scan/run` 着色）。

### 5.3 答题闸门（核心页）—— 严守 `questions.md` 格式契约

**机器校验契约**（来自 `validate-questions.py`，前后端都要遵守）：
- 第 1 行**必须** `# 待确认问题清单 — <工单号>`；有题时标题下必须有固定说明行 `> 请在每个"✅ 答案"下方填写确认结果；如果暂时无法确认，填写 \`[待确认]\`。`
- 无题时正文**只能**一行 `无`。
- 有题：连续 `## QN: <陈述>`（Q 从 1 连续递增，**不允许**其他 `##` 分节），每块**严格按序**含 5 字段：`**问题**：` `**来源**：` `**可能场景**：` `**影响范围**：` `**✅ 答案**：`。
- 答案区：人工答案 / 自动消解（`（据 <来源> 自动消解）`）/ HTML 注释占位 `<!-- 待填 -->` 三选一；needs_human 在答案上方加 `> 已自动检索无据，需人工/产品确认：…`。
- 禁 `<b>/<strong>/《span/HTML 实体/TODO`。

**前端做法**：
1. 解析 3 形态（新 Q-block / 自动消解 / **legacy markdown 表格**）渲染成结构化表单；**legacy 表格是 bug 高发解析点**，先支持 Q-block + 自动消解，legacy 用 `normalize-questions.py` 一键转规范后再编辑。
2. 用户只填/改**仍为占位**的题；已有人工/自动消解答案只读展示。
3. "自动消解预览"按钮：调 `normalize_text(path, text)`（纯函数、不落盘）出 before/after diff。
4. **保存 = 乐观并发 + 原子写 + 校验闸门**（任一不过则不落盘、回显错误）：

```python
def save_questions(path, new_text, client_mtime):
    if path.stat().st_mtime != client_mtime:        # 乐观并发
        raise Conflict("文件已被他人/批量更新，请刷新")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(new_text, encoding="utf-8")        # 临时文件
    issues = validate_questions.validate(tmp)         # import 直接校验
    if any(i.level == "FAIL" for i in issues):
        tmp.unlink(); raise ValidationError(issues)   # 不过 → 不落盘
    os.replace(tmp, path)                             # 原子替换
```

### 5.4 只读树视图 + 校验标记 + raw 编辑

- 渲染：长度=1 的数组 → 可展开思维导图；节点类型**按 marker key 推断**（container=只有 `children`；test-point=有 `mark`+`testPoint`，显示 P1/P2/P3 徽章；叶子 `condition/step/expect`）。HTMX 下用**一小块原生 JS**（或 `<details>` 折叠）渲染；不值得为此上 React。
- 校验标记：`Validator(path).validate()` 返回 `Issue{level, path(JSONPath), message}`；解析 `$[0].children[3].mark.priority` 走树定位，打**内联红(FAIL)/黄(WARN)**，并列可点击问题清单滚动定位。
- 编辑：**raw 文本面板**（CodeMirror/textarea）。保存 = **写原文字节**（仅做一次非破坏性 `json.loads` 合法性检查），**绝不经 Python 对象 round-trip**（防 key 重排/转义漂移）；FAIL 阻断保存、WARN 放行。结构化增改仍去 CodeArts。

### 5.5 实时进度

- **首选轮询**：受众小，`hx-trigger="every 2s"` 轮询 `_sprint-progress.json` 即可，零新后端管线。
- 子进程**最后一行 stdout** 也透出来（否则单工单跑几分钟会被误认为卡死）。
- 强模型审计因为是 SDK 流式，用 **SSE** 把 `log()` 事件推过去（`GET /jobs/{id}/events`）。

---

## 6. 建议目录结构（新增，不动现有）

```
qa-workflow/
├── scripts/                # 现有，不动（被 import / subprocess）
├── webapp/                 # 新增：前端服务
│   ├── main.py             # FastAPI app、路由、Job Manager
│   ├── deps.py             # auth/session、invoke() 包装、锁
│   ├── services/
│   │   ├── pipeline.py     # 包 run_sprint 子进程 + run_pipeline import
│   │   ├── validation.py   # import 5 个校验器（含 importlib 加载连字符文件）
│   │   ├── questions.py    # 解析/校验/原子写 questions.md
│   │   └── tree.py         # test-design.json 解析 + JSONPath 标记
│   ├── strong/             # 新增：Agent SDK 强模型端口
│   │   ├── spot_check.py   # qa-spot-check.js 的 Python 端口
│   │   ├── resolve.py      # qa-resolve.js 的 Python 端口
│   │   ├── tools.py        # recompute 数值 AST 求值器
│   │   └── schemas.py      # FINDINGS/VERDICT/RESOLVE/APPLY schema
│   ├── templates/          # Jinja2 + HTMX 片段
│   └── static/             # Pico.css/Tailwind CDN + 树视图 JS
├── docs/frontend-implementation-plan.md   # 本文件
└── pyproject.toml / requirements-web.txt  # 新增 web 依赖（与现有隔离）
```

> 连字符文件名（`validate-test-design.py` 等）无法普通 import，用 `importlib.util.spec_from_file_location` 加载（已验证：除 `sys.path` 外无 import 期副作用）。

---

## 7. 分阶段实施计划

> 估时按 1 名熟悉本仓库的开发者；多人需求把每阶段都抬高一档。

### M0 · 去风险 spike（1-2 天）
- [ ] `importlib` 加载 3 个可导入校验器（`validate-test-design.Validator`、`validate-questions.validate`、`normalize-questions.normalize_text`），确认无副作用。
- [ ] `invoke()` 包 `run_pipeline` 真跑一单，确认失败单**返回值**而非杀进程。
- [ ] `Popen(argv)` 跑 `run_sprint.py --until questions` 一单，确认 `_sprint-progress.json` + stdout 末行够做进度。
- [ ] 字节级 round-trip pytest：`json.dumps(ensure_ascii=False,indent=2)+"\n"` 对 3-5 个真实 `test-design.json` 断言字节一致。
- **产出**：go/no-go 结论 + 4 个通过的脚本/测试。

### M1 · 骨架 + 鉴权 + 看板（~1.5 周）
- [ ] FastAPI + Jinja2 + HTMX 骨架；**登录鉴权 + session**；内网 + TLS（反代）。
- [ ] sprint 看板（决策表 + 徽章 + 覆盖）；配置页（掩码只写）。
- [ ] Job Manager + 单产品串行锁 + SSE 进度通道。
- **产出**：能登录、看到 sprint 状态、看配置。

### M2 · 弱链驱动 + 答题闸门（~2 周）
- [ ] `生成 / 续跑(--resume-after-questions) / 选单(--select) / --until questions` 按钮 → 子进程 + SSE。
- [ ] **答题闸门**：解析 3 形态、`normalize_text` 预览、`validate` 硬闸、**乐观并发 + 原子写**、身份留痕。
- **产出**：每周流程不再手改 markdown 即可跑到 design。

### M3 · 只读树视图 + 校验（~1.5-2 周）
- [ ] 思维导图渲染 + JSONPath 内联 FAIL/WARN + 可点问题清单。
- [ ] raw 编辑面板（字节写 + 校验门）。
- **产出**：粘进 CodeArts 前能在 app 内查错。

### M4 · 强模型无头移植（~3-4 周）
- [ ] `strong/tools.py`：`recompute` 数值 AST 求值器 + 受限子进程兜底。
- [ ] **spot-check 端口先行**（只读）：5 维 `gather` + 对抗核验 + 误报过滤 + 确定性写 `_spot-check.md`。
- [ ] **resolve 端口**（写 `questions.md`）：3 阶段 + 人工答案不可改 + 确定性写回 + `.bak` + 写后校验回滚。
- [ ] **shadow-run 平价评审**通过后切流量；保留"复制命令"兜底。
- **产出**：审计步骤在 app 内一键跑，闭环。

### M5 · 收尾
- [ ] 审计日志、备份/提交按钮、错误页、并发压测（两人同改一单走锁/409）。

---

## 8. 项目特定的坑（务必规避）

1. **`SystemExit` 当错误**（`qa_pipeline.py:574` 等）：in-process 必 `try/except (SystemExit, RuntimeError)`；长任务宁可子进程隔离。
2. **`test-design.json` 字节级一致不可破**（粘进 CodeArts）：序列化 = `json.dumps(ensure_ascii=False, indent=2)+"\n"`；raw 编辑写原文、别让 JS/对象当序列化器。
3. **`questions.md` legacy 表格形态**最易解析出 bug，且它是唯一人工闸门：先 `normalize` 再编辑；保存必须"临时→校验→`os.replace`"，**绝不覆盖人工已填**。
4. **算术维度**移植**绝不给裸 Bash**（多人+客户数据=RCE/泄露）：用数值 AST 求值器。
5. **密钥绝不进浏览器**；**绝不**回显 `_load_env.py` 原始 stdout；配置页掩码只写。
6. **子进程 argv 列表**，永不 `shell=True`。
7. **进度粒度粗**：`_sprint-progress.json` 每完成一单才更新；务必同时透出子进程末行 + 计时，否则跑几分钟像卡死。
8. **多人写竞争**：工单级锁 + 乐观 `mtime`/hash；`build_ticket` 的 `--force` 会覆盖 `test-design.json`（先 `.bak`）——批量与人工编辑必须互斥。
9. **共享工作树 + git**：单实例独占；提交做成显式动作；别多实例对多 clone。
10. **Agent SDK 额度池**（2026-06-15 起独立计费）：强模型审计保持按需，UI 标注。

---

## 9. 技术选型清单

| 用途 | 选型 | 备注 |
|---|---|---|
| 后端框架 | **FastAPI + Uvicorn** | 与现有 Python 同栈，可直接 import `scripts/` |
| 模板 / 交互 | **Jinja2 + HTMX** | 零前端构建链 |
| 样式 | **Pico.css** 或 Tailwind(CDN) | 不引入构建链；要更强设计感再议 |
| 强模型 | **`claude-agent-sdk`（Python）** | 自动加载 `.claude`+`CLAUDE.md`+MCP；结构化输出/子 agent/权限 |
| 弱模型 | 现有 `cheap_model`（百炼 qwen，anthropic SDK） | 不动 |
| 鉴权 | `fastapi-users` 或自写 + 签名 cookie / 接 SSO | 小团队从简 |
| 校验器 | 现有 5 个，`importlib` 加载连字符文件 | 不重写规则 |
| 部署 | 内网服务器 + Nginx/Caddy(TLS) + systemd/NSSM | 单实例独占工作树 |
| **明确不用** | React / Vite / Tailwind 构建 / shadcn / Zustand / react-arborist / 数据库 / SSE 之外的消息队列 | 除非出现结构化拖拽编辑或更大规模并发 |

---

## 10. 待你拍板 / 澄清

1. **鉴权方式**：公司有没有可接的 SSO/LDAP？没有就用本地用户表 + 签名 cookie。
答：没有
2. **部署位置**：放哪台内网机器跑这个单实例？谁维护工作树的 git 提交（手动 vs app 内按钮）？
答：你决定，方便的就行
3. **Jira 身份**：全员共用一套只读服务账号即可（推荐），还是必须各用各的 Jira PAT？（后者才需 §3.2 的 env 注入小改造）
答：必须各用各的 Jira PAT
4. **强模型凭证/额度**：用哪个 `ANTHROPIC_API_KEY`/计划承接审计？是否接受 6-15 后的独立额度池？
答：按你的建议来就行，现在我使用的并不是官方的api或者模型，但是支持ANTHROPIC的api和模型，后续也是这样使用，这个需要自己配置api、apikey、模型等
5. **树编辑边界**：确认 v3 只做"只读视图 + raw 编辑"、结构化增改留 CodeArts？（这是控制工作量与风险的关键取舍）
答：按你的建议来就好了
