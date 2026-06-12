---
description: 需求理解 + 风险识别 + 待确认问题清单
allowed-tools: Read, Write, Edit, Glob, Grep, mcp__atlassian__*
---

# /qa:analyze

按 `prompts/01-analyze.md` 的完整规范执行：输出功能清单 + 风险分析 + 待确认问题 + 测试点数量预估。

**不要**在本步直接生成测试点列表（那是 `/qa:points` 的事）。

读取并严格遵循：
@prompts/01-analyze.md
@CLAUDE.md
@_kb/_global/markdown-artifact-schema.md
@_kb/_global/qa-methodology.md
