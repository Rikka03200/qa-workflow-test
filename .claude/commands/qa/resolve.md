---
description: 证据消解 + 证据完整性审查——人工回答前补充漏问、自动答有据问题，只留真无据的给人工
argument-hint: <EAR-xxxx[,EAR-yyyy...]> 或 <product> <sprint-date>
allowed-tools: Read, Bash, Glob, Workflow
---

# /qa:resolve — 强模型证据消解 + 证据完整性审查

> 用法：`/qa:resolve EAR-240883` 或 `/qa:resolve EAR-1,EAR-2`（多单）；也可 `/qa:resolve wms 2026-06-09`（整 sprint）
> 时机：弱链生成到 `questions` 之后、**人工回答之前**（即 `run_sprint --until questions` 之后）。
> 定位：基于**实际证据**做两件事：①把 `questions.md` 里有据可查的待确认点自动答掉（经对抗式核验）；②审查 `questions.md` 是否漏掉「写确定测试预期所必需、但证据仍不足」的业务事实，并在人工回答前补进去。**只把真正无据/需原型/需产品的留给人工**。
>
> 禁止：不做业务“预演”、不模拟系统行为、不按经验补规则；不从 `test-design.json` 收割 `[待确认]` 回填。

## 你（主 Claude）要做的

1. 解析参数：给工单号则用之；给 `<product> <sprint-date>` 则列该 sprint 目录下全部工单。默认 `product=wms`。定位每个工单目录为**绝对正斜杠**路径（如 `D:/Projects/qa-workflow/tickets/wms/2026-06-09/EAR-240883`）。只要存在 `questions.md` 就可跑：即使当前写“无”或既有问题都已答，也要做证据完整性审查，确认没有漏问。

2. 调用 **Workflow 工具**（脚本已就绪，勿重写）：
   ```
   Workflow({
     scriptPath: "D:/Projects/qa-workflow/.claude/workflows/qa-resolve.js",
     args: { ticketDirs: ["<每个工单的绝对正斜杠路径>"], product: "wms" }
   })
   ```
   它对每单：①用 L1 方案 + 关联工单评论 + `rules.md` + `_jira-search.md` + 同机制历史规则逐条试答 `questions.md` 既有待确认点；②基于这些**实际证据**审查是否漏问了「写确定测试步骤/预期所必需」的业务事实（不读 `test-design.json`，不做 JSON 收割）；③对每条「已据消解」做**对抗式核验**（证据不足则退回人工，防止消解自己幻觉）；④把经核验的答案和补充问题写回 `questions.md`（只填空、**绝不改动你已填的人工答案**），无据项保留占位并标「需人工/产品确认」，并生成 `<目录>/_resolve.md`。

3. 工作流返回每单 `{ear, added_questions, resolved_applied, needs_human, skipped_human_answered, notes}`。给用户一句话汇总：本批补充了多少个漏问、自动消解了多少条、还剩多少要人工回答（指明哪些单的哪些题）；提示用户：**现在只需回答各单 `questions.md` 里仍标「需人工」的少量问题**，回答后运行 `python scripts/run_sprint.py --product <product> --sprint <date> --select --resume-after-questions` 继续生成 points/design，再 `/qa:spot-check`。

## 注意
- 这是**强模型**环节：花主 Claude 额度、**不动百炼**。它是「弱链生成 → qa:resolve → 人工回答 → resume 生成 → 强抽检」流程中的消解步。
- **人工答案为终裁**：questions.md 中已有非注释答案的题，本步一律不动。
- **绝不为消解而编造**：证据不足、或答案依赖需登录原型图（mastergo/lanhu）、或需产品拍板的，一律留 `needs_human`，不得自行定值。
- **不做“预演”**：这里的完整性审查只检查实际证据是否足以支撑确定测试预期；不得模拟系统行为、不得按经验猜业务结果、不得补方案外功能。
- **不做 JSON 收割/回填**：本环节发生在人工回答前，只读证据与 `questions.md`；不得从 `test-design.json`、`test-points.md`、`_spot-check.md` 反向采集待确认。
- 自动消解的答案会带 `（据 <来源> 自动消解）`，便于你事后抽查；若发现某条自动答案与你的判断不符，直接改 `questions.md` 即可（人工优先）。
- 与 `/qa:spot-check` 的分工：resolve 在**人工回答前**预防（消解有据疑点），spot-check 在**生成后**纠错（兜底质检）。正常情况下经过 resolve + 人工 + 抽检三道，不应再出现「证据与人工答案冲突」；万一抽检后期撞上，只标记不自动改（人工优先）。
