# /qa:points — 测试点拆解

> 用法：`/qa:points`
> 前置：`/qa:analyze` + `/qa:resolve` 已执行；`questions.md` 中无未处理的有据问题，仍标「需人工」的已由用户回答，或用户明确允许保留 `[待确认]`。

---

## AI 任务

读取：

- `requirement.md`
- `business-context.md`
- `linked-issues.md`
- `analysis.md`
- `questions.md`（如存在）
- `_kb/_global/markdown-artifact-schema.md`
- `_kb/_global/case-writing-spec.md`
- `_kb/_global/qa-methodology.md`
- `_kb/projects/<product>/modules.md`
- `_kb/projects/<product>/case-samples/style-notes.md`

本步骤只输出测试点标题、优先级、覆盖依据；不要展开执行步骤和预期。

---

## 0. 检查 `questions.md`

- 如果 `questions.md` 不存在或内容为 `无` → 继续。
- 如果存在 Q1/Q2：
  1. 检查每个 `**✅ 答案**` 下方是否有非注释内容。
  2. 未填写时停止执行，并在聊天中提示未回答的问题编号。
  3. 用户明确说“暂时无法确认，先标 `[待确认]` 继续”时，才把对应答案写为 `[待确认]` 并继续。
- 用户在对话框回答但 `questions.md` 未填写 → 先询问是否同步到 `questions.md`，不要直接跳过表单。

---

## 1. 回填 `requirement.md` 的 Q&A 区

数据源优先级：

1. `questions.md` 已填写答案。
2. 用户在对话中给出的答案。
3. 都没有 → 维持 `[待确认]`。

回填到 `requirement.md ## 8. Q&A（来自 /qa:analyze 待确认问题）`，格式：

```markdown
### Q1: <问题陈述>

A: <用户答案或 [待确认]>
```

---

## 2. Pre-flight 5 问（必须写入 `test-points.md`）

写第一个测试点前，必须在 `test-points.md ## 1. Pre-flight 5 问` 中回答：

| # | 问题 | 通过条件 |
|---|---|---|
| 1 | 方案切面清单 | 能与 `analysis.md ## 2. 方案切面清单` 1:1 映射；用例数 ≈ 方案规则切面数（±2 条）；超出方案的切面禁止写 |
| 2 | CodeArts 存放目录依据 | 引用 `_kb/projects/<product>/modules.md §2 CodeArts 用例存放目录树` 或用户给的目录树 |
| 3 | 业务入口依据 | condition/step 入口来自方案、流程图、原型图、历史用例样本或已确认用户回答；找不到则标 `[待确认]` |
| 4 | 流程图交叉验证 | 有流程图则必须读；没有则写“不适用” |
| 5 | 公共页归属 | 公共页必须挂在宿主流程，不建独立容器路径 |

无法回答的项就是阻塞点；优先查证或标 `[待确认]`，不要硬写。

---

## 3. 写入 `test-points.md`

严格按 `_kb/_global/markdown-artifact-schema.md §9` 写入：

```markdown
# 测试点列表 — <ticket>

## 1. Pre-flight 5 问
## 2. 测试点列表
### 2.1 功能验证（主流程）
### 2.2 边界与字段校验
### 2.3 异常路径
### 2.4 权限矩阵
### 2.5 与历史功能联动
### 2.6 数据迁移 / 初始化
## 3. 容器路径
## 4. 不覆盖范围
## 5. 仍未决问题
```

### 3.1 测试点列表规则

- 只列测试点，不展开步骤和预期。
- 每条格式：`- P1  <测试点标题> —— <覆盖说明> [来源: ...]`。
- 没有内容的分组写 `无`，不要删除固定章节。
- 每条测试点必须能指向 `analysis.md ## 2. 方案切面清单` 中某一条，或明确是必要边界 / 历史联动。
- 不要机械套模板凑数；必要充分原则优先于覆盖维度模板。

### 3.2 命名硬约束

参考 `_kb/_global/case-writing-spec.md §1`：

- 格式：`{平台}_{模块路径}_{测试要点}`。
- 禁止 `场景1`、`场景 N`、`样例N`、`回归_EAR-xxxxxx`、`bug_xxxxxx`、`第一种`、`第二种` 等无信息占位。
- 多场景必须用业务差异点命名。
- 平台前缀大小写必须匹配产品规范：`web`、`仓配app`、`采配app`、`零售app`、`POS`、`TMS`。
- 字段名、参数名、按钮、选项值用中文双引号 `“...”`，不要用英文双引号或 markdown 反引号。

### 3.3 不覆盖范围

方案未提的维度默认不写测试点，并在 `## 4. 不覆盖范围` 说明：

- 权限。
- 导出 / 排序 / 列自定义。
- 修改记录。
- 必填 / 长度 / 字符集 / 重复校验。
- UI 文案精确校验。
- 非本次入口或非本次平台。

---

## 文件输出规则

- `test-points.md` 必须遵循 `_kb/_global/markdown-artifact-schema.md`。
- 完成后报告只输出在聊天回复中，不写入 `test-points.md`。
- 禁止在文件正文写 `✓ 共拆解`、`请审增删`、`确认后运行 /qa:skeleton`。
- 禁止使用 `<b>`、`<strong>`、损坏 span、HTML 实体转义。

---

## 完成后聊天报告

回复用户时只简述：

```text
已生成 test-points.md：共 N 个测试点（P1 a / P2 b / P3 c），仍未决 K 个。
请审测试点范围；确认后可运行 /qa:skeleton。
```
