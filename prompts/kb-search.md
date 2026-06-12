# /qa:kb-search — 跨产品检索本地 + Confluence

> 用法：`/qa:kb-search <keyword>` 或 `/qa:kb-search <keyword> --product=wms`

---

## AI 任务

参数：`keyword`（必填）、`product`（可选，限定产品线）。

### 1. 本地 KB 检索

执行（按以下范围 + 顺序）：

- `_kb/_global/**.md`
- `_kb/projects/<product>/**.md`（如指定）或 `_kb/projects/**/**.md`（全部产品）
- `tickets/<product>/**/business-context.md`（已沉淀过的工单上下文也是知识源）

用 Grep 工具搜 `keyword`，输出命中段落（带文件路径+行号）。

### 2. Confluence 检索（MCP）

- 调 `mcp__atlassian__confluence_search`，query = keyword
- 范围：`config.products.<product>.confluence_spaces`（如限定）或全部
- 取 top 10
- 列出标题 + URL + 摘要（不展开正文）

### 3. Jira 检索（可选）

如果 keyword 看起来像工单号（`EAR-\d+`）或用户加了 `--jira`：
- 调 `mcp__atlassian__jira_get_issue` 或 `mcp__atlassian__jira_search`
- 列出命中工单

### 4. 凭证缺失降级
- env 缺 → 跳过远端，仅本地结果

---

## 输出

```markdown
# 搜索 "<keyword>" 结果

## 本地 KB（N 条）

### _kb/projects/wms/rules.md
- §4.2 模块顶层注释（line 76）：
  > 开启"允许跨波次领取任务"，不应用"批次推荐规则"...
- §4.5（line 95）：...

### _kb/projects/wms/terms.md
- 表格行（line 23）：...

### tickets/wms/2026-06-02/EAR-245xxx/business-context.md
- §3 涉及的业务规则（line 45）：...

## Confluence（M 条）

| # | 页面 | Space | URL |
|---|---|---|---|
| 1 | WMS 拣货规范 | WMS | https://... |
| 2 | ... | | |

## Jira（K 条）

| # | Issue | 标题 | 状态 |
|---|---|---|---|
| 1 | EAR-246155 | 批次推荐规则 | Done |

## 总结

- 本地权威结论：<如有共识>
- 仅 Confluence 提及：<如有>
- 不一致点：<如有冲突，列出>
```

---

## 抗幻觉硬规则

1. 只输出**实际命中**的内容，不要"看起来相关"地补
2. 摘要保留原文片段，不要概括
3. Confluence 不接通时明确说"未检索远端"

---

## 用法示例

```
/qa:kb-search 批次推荐规则
/qa:kb-search 库存形态 --product=wms
/qa:kb-search EAR-246155
/qa:kb-search 不锁库 --jira
```
