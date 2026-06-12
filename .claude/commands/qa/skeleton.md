---
description: 生成测试设计 JSON 骨架（仅到步骤标题）
allowed-tools: Read, Write, Edit, Glob, Grep
---

# /qa:skeleton

按 `prompts/04-skeleton.md` 的完整规范执行：基于确认的测试点列表，生成 JSON 骨架（完整层级 + 前置 + 步骤 text，**不**含 expect），写到 `test-design.json`。

读取并严格遵循：
@prompts/04-skeleton.md
@CLAUDE.md
@_kb/_global/codearts-json-schema.md
@_kb/_global/case-writing-spec.md
