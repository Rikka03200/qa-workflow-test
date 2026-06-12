---
description: Sprint 批量端到端编排：选单→生成→证据消解→人工答少量→续跑→强抽检，一条命令自动串联（中间仅一次人工答题闸口）
argument-hint: <product> <sprint-date> [--run] [--dry-run]
allowed-tools: Read, Bash, Glob, Workflow, Write
---

# /qa:sprint $ARGUMENTS

按 `CLAUDE.md §4.5 Sprint 批量选单规则` 选取并驱动整个 sprint。规则**确定性、已固化在脚本里**，不要凭印象挑单、不要自行增删条件。本命令是**端到端编排器**：自动串联「选单 → 生成到 questions → 证据消解 → 人工答少量 → 续跑生成 → 强抽检」，**中间只有一次不可避免的人工答题闸口**（你回答各单仍无据的少量问题）。

## 参数解析
- `product`：第一个参数（默认 `wms`）。`sprint-date`：第二个参数（如 `2026-06-09`）。
- `--run`：选完直接驱动后续全流程；不带则只选单出报告，等用户确认。
- `--dry-run`：仅选单（等价于不带 `--run`）。

## 你（主 Claude）要按顺序自动执行（除非用户只要选单）

**步骤 1 · 选单（只读）**
```bash
python scripts/select_sprint.py --product <product> --sprint <sprint-date>
```
读报告 `tickets/<product>/.sprint-state/_selection-<sprint-date>.md`，向用户简述：JQL 命中 / 候选数 / 将运行 N 单 / 跳过 M 单 + run_list + 任何「需人工复核」（拆单未匹配主单）。
- run_list 为空 → 提示「可能已全部走过或被开发置为已修复」，停。
- 未给 `--run` 且用户未明确同意 → 停在这里等确认（后续步骤会耗弱模型 + 主 Claude 额度）。

**步骤 2 · 生成到 questions（弱模型，不耗主 Claude）**
```bash
python scripts/run_sprint.py --product <product> --sprint <sprint-date> --select --until questions
```
只生成 `fetch→requirement→jira-search→context→linked-issues→analyze→questions`，**停在人工/消解前**（不生成 points/design，不记覆盖账本）。run_list 取步骤 1 的结果。

**步骤 3 · 证据消解 + 完整性审查（强模型，自动答有据的）**
对 run_list 执行 `/qa:resolve <run_list>`（即调用 `.claude/workflows/qa-resolve.js`）。它只基于实际证据（L1 方案 + 关联工单 + `rules.md` + `_jira-search.md` + 同机制历史规则）做两件事：①自动答掉**有据可查**的既有待确认点（经对抗式核验）；②在人工回答前审查 `questions.md` 是否漏掉「写确定测试步骤/预期所必需」的业务事实，并把真无据的漏问补进 `questions.md`。它不做业务“预演”、不模拟系统行为、不从 `test-design.json` 收割 `[待确认]`。最终只把**真无据/需原型/需产品**的留给人工，生成各单 `_resolve.md`。

**步骤 4 · 人工答题闸口（必停）**
汇总：本批自动消解 X 条、仍需人工 Y 条，**逐单列出仍标「需人工」的具体问题**。然后**停下**，请用户回答这些问题（直接填各单 `questions.md` 的 `✅ 答案`，或在对话里答、你再回填）。这是流程里唯一的人工介入点——有据的已不再问。若 Y=0（全部有据已消解），跳过本闸口直接进步骤 5。

**步骤 5 · 续跑生成（用户答完后，弱模型）**
```bash
python scripts/run_sprint.py --product <product> --sprint <sprint-date> --select --resume-after-questions
```
保留 requirement/context/analyze/questions，只重做 `points→design→packet`，把人工+消解的答案吃进最终 `test-design.json`。**必须带 `--select`**：它把续跑范围严格限定为步骤1 的 run_list（不波及 sprint 目录里的遗留/非选中单），并在 design 生成后登记覆盖账本 `coverage-ledger.json`（§4.5.3 跨 sprint 去重的权威记录；漏了 `--select` 账本就拿不到 source=run 记录）。读看板 `_sprint-summary-<sprint-date>.md` 汇报。

**步骤 6 · 强抽检（强模型，自动）**
若看板中运行单全部 `JSON校验=PASS` 且 `产物校验=PASS`，**不要停在「建议下一步」**，直接 `/qa:spot-check <run_list>`，把结果写入各单 `_spot-check.md`，汇总确认问题。仅当要按抽检建议改 JSON / 重生成 / 提升黄金范例时才再次征求确认。
- 有单未过校验 → 先报失败单/阶段/错误，不对失败单抽检；修复或重跑通过后再抽检。

## 注意
- **自动串联但有一个必停点**（步骤 4 人工答题）——这是设计如此：消解把有据的都答了，只有真无据的才需要你。其余步骤无需你逐条点头。
- 额度：步骤 2/5 走弱模型（百炼）；步骤 3/6 走强模型（主 Claude）。`--run` 即视为对全流程额度的同意。
- **待确认消解顺序是硬规则**（`CLAUDE.md §4 SOP` + §4.5.4）：先 `/qa:resolve` 再人工，有据问题不许进人工问答；若 `questions.md` 漏掉了写确定预期所必需的业务事实，也必须在人工回答前由 `/qa:resolve` 基于实际证据补进 `questions.md`。人工答案为终裁；后期抽检若再撞冲突只标记不自动改。
- 简化用法：待确认极少/全为「无」的批次，可不分段，直接 `run_sprint --select`（一把梭到 design）+ `/qa:spot-check`；但默认推荐上面的分段编排。
- 选单**只读 Jira、零产物、零额度**；看板/账本/报告都在 `tickets/<product>/.sprint-state/`。看板 id 解析不到时让用户在 config 填 `jira_board_id`（WMS=236）或加 `--board`。
- 选取条件、拆单规则**唯一真源是 `CLAUDE.md §4.5` 与脚本**；改条件改 `config.local.yaml` 的 `selection:` 段，不要在对话里临时加。
