---
description: 测试点拆解（标题+优先级，不展开步骤）
allowed-tools: Read, Write, Edit, Glob, Grep
---

# /qa:points

按 `prompts/03-test-points.md` 的完整规范执行：基于已回答的待确认问题，输出测试点列表（5 个分组），并把用户答案回填到 `requirement.md` 的 Q&A 区。

读取并严格遵循：
@prompts/03-test-points.md
@CLAUDE.md
@_kb/_global/markdown-artifact-schema.md
@_kb/_global/case-writing-spec.md
@_kb/_global/qa-methodology.md
