---
description: 检索 KB + Confluence，写入 business-context.md
allowed-tools: Read, Write, Edit, Glob, Grep, mcp__atlassian__*
---

# /qa:context

按 `prompts/02-context.md` 的完整规范执行：识别当前工单 → 提取关键词 → 摘录本地 KB → Confluence 检索 → 输出 `business-context.md`。

读取并严格遵循：
@prompts/02-context.md
@CLAUDE.md
@_kb/README.md
@_kb/_global/markdown-artifact-schema.md
