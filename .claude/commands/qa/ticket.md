---
description: 单工单端到端编排：生成到 questions→证据消解/补漏问→人工答一次→续跑生成→强抽检
argument-hint: <product> <EAR-xxxx> [--sprint <date>] [--run]
allowed-tools: Read, Bash, Glob, Workflow, Write
---

# /qa:ticket $ARGUMENTS

单工单版 `/qa:sprint`。用于只跑一个工单时，仍按同一套标准流程执行：

```text
fetch → requirement → jira-search → context → linked-issues → analyze → questions
→ /qa:resolve 证据消解 + 漏问审查
→ 人工只答一次 questions.md
→ points/design/packet
→ /qa:spot-check
```

不要因为只跑一个工单就跳过 `jira-search`、`/qa:resolve`、`questions.md` 稳定校验或强抽检。

## 参数解析

- `product`：第一个参数，默认 `wms`。
- `EAR-xxxx`：工单号。
- `--sprint <date>`：可选。若给出，直接使用 `tickets/<product>/<date>/<EAR>/`；不传则由 `qa_pipeline.py` 从 Jira Sprint 字段推导，或复用本地已有目录。
- `--run`：直接驱动全流程；不带则先说明将执行的步骤，等用户确认。

## 你（主 Claude）要按顺序执行

### 步骤 1 · 生成到 questions（弱模型）

```bash
python scripts/qa_pipeline.py <EAR-xxxx> --product <product> [--sprint <date>] --only fetch,requirement,jira-search,context,linked-issues,analyze,questions --redo questions
```

说明：
- 生成/更新到 `questions.md` 后停止。
- `qa_pipeline.py` 会自动执行 `normalize-questions.py` + `validate-questions.py`，确保 `questions.md` 格式稳定；格式自动修不好则阻断，不进入后续。
- 本步会生成 `_jira-search.md`，用于后续证据消解。

### 步骤 2 · 证据消解 + 完整性审查（强模型）

对该工单目录执行 `/qa:resolve <EAR-xxxx>`（即调用 `.claude/workflows/qa-resolve.js`）。

它只基于实际证据做两件事：
1. 自动答掉 `questions.md` 里有据可查的问题；
2. 在人工回答前审查是否漏掉「写确定测试步骤/预期所必需」的业务事实，并把真无据漏问补进 `questions.md`。

禁止：不做业务“预演”、不模拟系统行为、不从 `test-design.json` 收割 `[待确认]`。

### 步骤 3 · 人工答题闸口（必停）

汇总该单仍需人工的问题。若仍有需人工项，停下让用户只回答该工单 `questions.md` 的 `✅ 答案`。

用户答完后说“继续”，再进入步骤 4。

若需人工项为 0，直接进入步骤 4。

### 步骤 4 · 续跑生成 points/design/packet（弱模型）

```bash
python scripts/qa_pipeline.py <EAR-xxxx> --product <product> [--sprint <date>] --only points,design,packet --redo points,design,packet
```

说明：
- 保留 requirement/context/analyze/questions。
- 只重做 `test-points.md`、`test-design.json`、`_qa-packet.md`。
- 进入 points/design 前会再次归一并校验 `questions.md` 格式。

### 步骤 5 · 强抽检（强模型）

若步骤 4 通过校验，直接执行：

```text
/qa:spot-check <EAR-xxxx>
```

把结果写入该工单 `_spot-check.md` 并汇总确认问题。只有后续要按抽检建议改 JSON / 重生成 / 提升黄金范例时才再次征求确认。

## 注意

- 单工单与 sprint 批量使用同一套质量规则：先证据消解，再人工，只答一次 `questions.md`。
- `questions.md` 是唯一人工答题入口；不建议在对话框里逐题回答。
- 如果已有人工答案，`/qa:resolve` 不会改动。
- 若本地已有旧格式 `questions.md`，流水线会先自动归一；归一后仍不合规则阻断并报告具体错误。
- 若未提供 `--sprint` 且本地存在多个同名工单目录，先列出候选路径并请用户确认，不要凭猜测选目录。
