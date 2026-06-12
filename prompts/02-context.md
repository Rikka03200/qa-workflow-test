# /qa:context — 业务上下文检索与摘录

> 用法：`/qa:context`（在已有工单目录的工作树中）
> 前置：`/qa:new` 已执行，`requirement.md` 已就绪

---

## AI 任务

### 1. 识别当前工单

- 从当前工作目录或最近修改的 `tickets/<product>/<sprint-date>/<ticket>/` 推断，其中 `<sprint-date>` 为 Jira Sprint 日期（如 `2026-06-02`），`<ticket>` 为工单号（如 `EAR-240953`）。
- 兼容读取历史旧目录 `tickets/<product>/<YYYY-Www>_<ticket>/`，但新产物必须写入 sprint 日期分组目录。
- 找不到唯一目录 → 询问用户指定。

### 2. 扫读 `requirement.md`

- 优先读取 `## 3. 修改方案（L1 业务规则真源）`，关键词识别和业务规则摘录都从本段出发。
- `## 2. 客户与背景`、`## 5. 评论关键摘录`、`## 6. 原型图链接` 仅用于：
  - 提取边界场景关键词，进入 `analysis.md` 风险清单。
  - 提取客户/账套上下文，判断是否定制规则适用。
  - 提取原型图链接，提示用户自行获取。
- 客户编号 / 正式账套号 / 租户名只作背景理解，**不得转写进 `business-context.md §7 用例编写约束` 的前置条件要求，也不得作为后续用例 condition**。定制客户/单账套工单在测试环境按普通用例对待，前置只写可执行业务数据条件。唯一例外：L1 方案明确"按账套/租户生效/灰度/隔离/可见性差异"时，写明来源后才作为业务规则。
- 禁止把 L2/L3 内容当作规则关键词去 `rules.md` 检索摘录。
- 提取本次需求涉及的关键词：模块名、字段名、参数名、术语；核心关键词仅来自 L1。

### 3. 从本地知识库摘录

按顺序摘录：

1. `_kb/projects/<product>/terms.md` — 涉及的术语定义。
2. `_kb/projects/<product>/modules.md` — CodeArts 容器路径、系统菜单 / 操作短路径。
3. `_kb/projects/<product>/rules.md` — 涉及的业务规则，整段摘录，不重写。
4. `_kb/_global/case-writing-spec.md` — 始终引用命名、前置条件、步骤、预期规则。
5. `_kb/_global/codearts-json-schema.md` — 始终引用 JSON 层级和自查规则。
6. `_kb/_global/markdown-artifact-schema.md` — 始终引用 Markdown 产物结构与禁止内容。

每条摘录后必须标 `[来源: 文件 §章节号]`。

### 4. 从 Confluence 检索（MCP）

- 调用 Confluence MCP，query = 关键词列表。
- 范围：`config.products.<product>.confluence_spaces`。
- 取 top 5 命中页面，拉正文。
- 只提取与本次需求直接相关的段落，不要整页粘贴。
- 标明来源：`[来源: Confluence "<page title>" <section>]`。

### 5. 关联工单全量阅读（必须产出 `linked-issues.md`）

- 读取 `requirement.md` 中 `## 4. 关联工单` 列出的所有工单。
- 对每条关联工单调用 `mcp__atlassian__jira_get_issue`，`fields="summary,status,issuetype,priority,description,attachment,issuelinks,comment,customfield_10020"` 或 `fields="*all"`，`comment_limit` ≥ 30。
- 必读：description、所有评论、attachments 元数据、resolution。
- 仅当 resolution 明确为 `Won't Do` / `Won't Fix` / `Duplicate` / `Cannot Reproduce` 时可只记录标题；其余状态必须读完。
- `linked-issues.md` 必须严格按 `_kb/_global/markdown-artifact-schema.md §5` 写入。
- 无关联工单时仍生成 `linked-issues.md`，写明“无”。
- 关联工单的“修改方案”段是 L1；产品作者在评论中明确写“补充方案 / 方案更新 / 方案修订 / 方案确认”的内容也是 L1；其余按 L2/L3 处理。
- 关联工单中的 L1 业务规则必须进入 `business-context.md ## 3.2 关联工单 L1 规则`。

### 6. 业务规则 Jira 搜索（标 `[待确认]` 前必须走）

关键词清单中任何业务规则类未知项（算法、参数行为、模块定义、术语），先用 `mcp__atlassian__jira_search` 多维度迭代检索。

必跑 5 个维度：

| # | 维度 | JQL 模板 |
|---|---|---|
| 1 | component + 核心关键词 | `project = EAR AND component = "<comp>" AND text ~ "<核心词>" ORDER BY updated DESC` |
| 2 | summary 精确匹配 | `project = EAR AND summary ~ "<功能名>" ORDER BY updated DESC` |
| 3 | 拆词搜索 | `project = EAR AND text ~ "<词1>" AND text ~ "<词2>"` |
| 4 | 收敛到已解决 + 已修复 | `... AND resolution = "已修复" ORDER BY updated DESC` |
| 5 | 横向相邻模块/概念 | `project = EAR AND summary ~ "<相邻模块>" AND status = "已解决"` |

关键词扩展：中文 / 英文 / 拼音 / 缩写各试一遍；同义词如“分配 / 分摊 / 派发 / 指派”、“覆盖 / 替换 / 重写”、“忽略 / 跳过 / 排除”。

命中处理：

- 命中 ≤ 3 条 → 全读 description + 评论。
- 命中 4~10 条 → 按 summary 相关性挑 top 5 全读。
- 命中 > 10 条 → 优先选 `resolution = "已修复"` 且 summary 关键词匹配度高的工单。

命中后把相关工单摘录到 `linked-issues.md ## 2. 参考工单（Jira 搜索命中）`，并在 `## 3. 已搜索 JQL` 记录 JQL 与命中数。

只有 5+ 条精细 JQL 都无收获时，才在 `business-context.md ## 5. Jira 搜索记录` 记录无结果，并允许后续 `analysis.md` 标 `[待确认]`。

### 7. 截图 ≠ 原型图

- Jira 工单中嵌入图片默认不是原型图；最多作为佐证。
- 原型图通常以独立链接写在工单描述中，如 `mastergo.com` / `lanhuapp.com` / `edrawmax.cn`，需用户登录访问。
- UI 元素文案（按钮/弹窗/标题/菜单）用功能性描述直接写，**不标 `[待确认]`**（不纠结精确措辞，详见 `_kb/_global/case-writing-spec.md §5`）；只有**影响测试判断的字段位置/控件存在性**在方案与原型均未明确时，才标 `[待确认: 以原型图为准]`。

### 8. 写入 `business-context.md`

严格按 `_kb/_global/markdown-artifact-schema.md §6` 写入。

必须包含：

- `## 0. 关键词识别`
- `## 1. 涉及的术语`
- `## 2. 涉及的模块路径`
- `## 3. 涉及的业务规则`
- `## 4. Confluence 检索结果`
- `## 5. Jira 搜索记录`
- `## 6. 与历史规则的关系 / 潜在冲突`
- `## 7. 用例编写约束`

没有内容的章节写 `无`。不要写“待执行”“待回填”。

### 9. JSON 节点 text 自包含原则

`business-context.md` 可以保留来源和证据；但 `/qa:skeleton` 与 `/qa:detail` 产出的 `test-design.json` 中，所有 `condition` / `step` / `expect` 节点 text 必须自包含，禁止出现：

- 工单号。
- 附件文件名。
- `[来源: ...]`、`requirement.md`、`§修改方案` 等文档引用。
- `已搜索 JQL`、`需求方案写...` 等 AI 工作过程。

---

## 文件输出规则

- `linked-issues.md` 和 `business-context.md` 必须遵循 `_kb/_global/markdown-artifact-schema.md`。
- 完成后报告只输出在聊天回复中，不写入 Markdown 产物。
- 禁止在文件正文使用 `<b>`、`<strong>`、损坏 span、HTML 实体转义。

---

## 完成后聊天报告

回复用户时只简述：

```text
已生成 business-context.md 和 linked-issues.md：本地摘录 N 条，Confluence 页面 M 个，Jira 参考工单 K 个，待确认问题 L 个。
下一步建议运行 /qa:analyze。
```
