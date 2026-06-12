---
description: 评审后从工单产物提炼新业务规则建议入库
allowed-tools: Read, Write, Edit, Glob, Grep
---

# /qa:kb-extract

按 `prompts/kb-extract.md` 的完整规范执行：扫读工单产物 → 识别可入库规则/术语/反例 → 输出提案 `kb-proposal.md` → 等待用户审批后写入对应 `_kb/` 文件并追加变更记录。

读取并严格遵循：
@prompts/kb-extract.md
@CLAUDE.md
@_kb/README.md
