# qa-workflow · AI Agent 契约（Codex 入口）

> Codex CLI 默认读取本文件作为项目级 AI 指令。**项目契约以 [`CLAUDE.md`](./CLAUDE.md) 为唯一真源**；本文件是 Codex 入口指针（角色契约、抗幻觉规则、选单规则等全部内容均见 `CLAUDE.md`，勿在此另写一份以免不一致）。

详见 [`CLAUDE.md`](./CLAUDE.md)。

---

## Codex 专用提示

Codex CLI 不支持 `.claude/commands/` 形式的 slash 命令。要执行工作流步骤，请按 SOP 顺序手动引用 prompt（注意：文件编号 ≠ 执行顺序，context 先于 analyze）：

```
@prompts/new-ticket.md        # 建目录 + 拉 Jira（对应 /qa:new）
@prompts/02-context.md        # 业务上下文 + 关联工单（对应 /qa:context）
@prompts/01-analyze.md        # 风险 + 待确认问题（对应 /qa:analyze）
@prompts/03-test-points.md    # 测试点（对应 /qa:points）
@prompts/04-skeleton.md       # JSON 骨架（对应 /qa:skeleton）
@prompts/05-cases-detail.md   # 补全预期（对应 /qa:detail）
@prompts/kb-extract.md        # 评审后知识沉淀（对应 /qa:kb-extract）
@prompts/kb-search.md         # 跨产品检索（对应 /qa:kb-search）
```

或直接 `cat prompts/02-context.md` 复制内容到对话框。

> **注意**：`/qa:resolve`（证据消解）与 `/qa:spot-check`（强抽检）是 Claude Code 专属 workflow（`.claude/workflows/`），**无独立 prompt 文件**。Codex 下需按 `CLAUDE.md §4 SOP` 手动完成「人工回答前先消解有据待确认」「生成后强抽检」两步，或改用 Claude Code 运行这两步。Bug 提交 / 验收评论等 Jira 写回动作已从工作流移除（见 `CLAUDE.md §5`），需要时直接在 Jira 网页操作。

Jira / Confluence MCP 在 Codex 中通过 `.mcp.json` 同样生效（Codex CLI 0.21+ 支持 MCP）。
