---
description: 初始化工单：建目录、拉 Jira、填 requirement.md
argument-hint: <product> <ticket-key>
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, mcp__atlassian__*
---

# /qa:new $ARGUMENTS

按 `prompts/new-ticket.md` 的完整规范执行：
- 参数：`product` = 第一个参数（如 `wms`），`ticket` = 第二个参数（如 `EAR-246155`）
- 校验产品线 → 拉 Jira 并识别 Sprint 日期 → 建目录 → 写 `requirement.md`

读取并严格遵循：
@prompts/new-ticket.md
@CLAUDE.md
@_kb/_global/markdown-artifact-schema.md
@_kb/_global/case-writing-spec.md
