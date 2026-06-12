# /qa:new — 工单初始化

> 用法（Claude Code）：`/qa:new <product> <ticket-key>`，例：`/qa:new wms EAR-246155`
> 用法（Codex / 手工）：把本文件喂给 AI，加上参数 `product=wms`、`ticket=EAR-246155`。

---

## AI 任务

参数：`product`、`ticket`。

执行以下动作：

### 1. 校验产品线

- 读 `config/config.local.yaml` 的 `products.<product>` 节。
- 不存在 → 报错并提示用户列出 `products:` 下所有可用 key。

### 2. 拉取 Jira 工单并识别 Sprint 日期

- 调用 `mcp__atlassian__jira_get_issue`，传 `issue_key = <ticket>`。
- `fields` 必须显式包含 `comment` + `customfield_10020`（问题测试员）+ `customfield_10125`（Sprint）：
  `summary,status,issuetype,priority,assignee,reporter,created,updated,labels,components,fixVersions,description,attachment,issuelinks,duedate,comment,customfield_10020,customfield_10125`
- `comment_limit` 设为 ≥ 30。
- 提取：summary、description、issuetype、priority、assignee、reporter、问题测试员、Sprint、status、labels、components、fixVersions、attachments 元数据、links、comments。
- 若返回未含 `comments` 字段或为空数组：重试一次 `fields="*all"` 确认，避免漏读。
- 从 `customfield_10125` 提取 `sprint-date`：
  - 优先解析 Sprint 名称中的日期前缀，如 `name=2026-06-02.BETA` → `2026-06-02`。
  - 若名称没有日期，解析 `endDate=YYYY-MM-DD...`。
  - 若有多个 Sprint，优先取 `state=ACTIVE`；否则取 `endDate` 最新的一条。
  - 若 Jira 未返回 Sprint 或无法解析日期，优先用 `config.local.yaml` 的 `workflow.current_sprint_date`；仍为空则停止并询问用户，不要用系统周次代替。

### 3. 建工单目录

```text
tickets/<product>/<sprint-date>/<ticket>/
├── README.md
├── requirement.md
├── business-context.md   # 占位，后续 /qa:context 覆盖
├── test-design.json      # 空占位 []
└── attachments/          # 空目录占位
```

如果目录已存在，报错并询问是否覆盖；不要默认覆盖。

### 4. 写入 `requirement.md`

严格按 `_kb/_global/markdown-artifact-schema.md §4` 写入 `requirement.md`。

#### 4.1 分级规则

工单类型为 `提高` / `新功能` / `需求` 时，description 通常包含背景 / 客户场景 / 期望 / 修改方案。只有“修改方案”段是业务规则真源。

写入时必须：

- L1 段：识别 `h4. 修改方案`、`h4. 修改方案（产品名）`、`h4. 解决方案`、`h3. 方案`、`## 修改方案` 等标志，写入 `## 3. 修改方案（L1 业务规则真源）`。
- L2/L3：客户信息、客户场景、期望目标、用户场景、业务背景写入 `## 2. 客户与背景（L3，仅用于理解上下文）` 或 `## 5. 评论关键摘录（L2/L3，用于风险判断，不作规则真源）`。
- 关联工单只写元数据到 `## 4. 关联工单`；完整内容由 `/qa:context` 写入 `linked-issues.md`。
- 原型链接与附件元数据分别写入 `## 6. 原型图链接`、`## 7. 附件元数据`。
- 找不到明确“修改方案”标志时，在 `## 3. 修改方案（L1 业务规则真源）` 写 `[待确认: 本工单 description 无明确“修改方案”段，请人工确认 L1 范围]`，不要自行框选某段当作 L1。

`Bug` / `Defect` 类工单不适用上述 L1 识别；规则真源在复现步骤、修复方案评论和关联缺陷说明中，仍需按契约分区写明。

#### 4.2 Jira wiki 转 Markdown 规则

- `requirement.md` 只能有第 1 行一个一级标题 `# <ticket>: <summary>`。
- Jira wiki 中的 `# 客户场景`、`# 修改方案` 等原始一级标题必须降级为当前章节下的普通段落或三级标题，不能破坏文档结构。
- 描述中的字段引用、菜单路径、数值原样保留；不要翻译或规范化。
- 固定章节没有内容时写 `无`，不要写“待回填”。

### 5. 记录附件元数据

- 只记录 Jira 返回的附件元数据：文件名、大小、类型、作者、创建时间、Jira URL。
- 不下载附件，不把图片/视频/Excel 保存到 `attachments/`。
- Jira 截图不是原型图；原型图通常是独立链接（如 `mastergo.com` / `lanhuapp.com` / `edrawmax.cn`），需要用户自行打开。

### 6. 写入 `README.md`

严格按 `_kb/_global/markdown-artifact-schema.md §3` 写入 `README.md`，包含 SOP 进度、关键决策记录、附加材料。

### 7. 凭证缺失降级

- 若 env 中 `JIRA_URL` / `JIRA_PERSONAL_TOKEN` 为空 → 只建目录与占位文件，并提示用户手工填 `requirement.md`。
- 即使降级，也必须按 Markdown 产物契约写出空模板，不要生成自由格式文件。

---

## 文件输出规则

- 文件正文必须严格遵循 `_kb/_global/markdown-artifact-schema.md`。
- 完成后报告只输出在聊天回复中，不写入任何 Markdown 产物。
- 不在文件中写 `✓ 已创建`、`下一步：/qa:context`、`请确认` 等聊天式提示。

---

## 完成后聊天报告

回复用户时只简述：

```text
已创建 tickets/<product>/<sprint-date>/<ticket>/，包含 README.md、requirement.md、business-context.md 占位、test-design.json 占位、attachments/。
下一步建议运行 /qa:context。
```
