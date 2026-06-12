# /qa:detail — 用例细化（补全预期）

> 用法：`/qa:detail`
> 前置：`/qa:skeleton` 输出已经过你确认

---

## AI 任务

读 `tickets/<product>/<sprint-date>/<ticket>/test-design.json` 骨架，给每个 `step` 节点补全 `expect` 子节点。

### 操作

- 每个 `step` 节点的 `children` 数组：从 `[]` 改为 `[{ id, text, expect: "Y" }]`。
- 不动测试点、CodeArts 用例存放目录容器、前置条件节点。
- 输出并写回完整、合法、纯 JSON 的 `test-design.json`。
- 禁止把自查报告、完成说明、Markdown 代码块围栏追加到 `test-design.json` 文件内。

### 预期 text 编写要求

参考 `_kb/_global/case-writing-spec.md §5`：

- 可校验：含具体字段名、值、状态变化；提示/UI 文案用功能性描述。
- 字段长度 / 字符集等校验，预期写明边界值与功能性提示，如 `输入21字符报错，提示最多20个字符`（不纠结确切措辞、**不标 `[待确认]`**）。
- 提示语：方案给出确切文案则精确引用原文；未给的用功能性描述（如 `提示编码重复，保存被拦截`），**不标 `[待确认]`**（UI/提示文案见 `_kb/_global/case-writing-spec.md §5`）。
- 与业务规则相关的预期，必须来自 `requirement.md` / `business-context.md` / `linked-issues.md` / `_kb/` 已确认规则。

### 优质预期示例

| 步骤 | 预期 |
|---|---|
| `wms-批次推荐规则，未勾选规则` | `不允许点击“删除”`（按钮置灰）|
| `选择规则1、规则2，点击删除` | `显示“批量删除”弹窗` |
| `填写规则代码=001，保存（已存在 code=001 的规则）` | `触发重复校验，提示代码重复、保存被拦截` |
| `登录用户B仓配App-拣货，点击领取` | `领取成功，拣货录入显示分拣库位=B-01，生产日期=2025-01-01，批次可拣量=100公斤` |
| `档案-角色管理，关闭：WMS-批次推荐规则-编辑` | `wms-批次推荐规则-编辑，保存按钮置灰` |

### 反面教材（禁止输出）

| 步骤 | 错误预期 |
|---|---|
| `点击保存` | `成功` |
| `输入超长字符` | `报错` |
| `批量删除` | `按预期执行` |

---

## JSON 输出规则

完整 JSON 写回：

```text
tickets/<product>/<sprint-date>/<ticket>/test-design.json
```

必须满足 `_kb/_global/codearts-json-schema.md`：

- 单根树。
- 所有 `id` 和 `testPoint.id` 是 32 位大写十六进制 UUID。
- 每个测试点有 `mark.priority` 和 `testPoint.id`。
- 每个 `step` 恰好 1 个 `expect`。
- `condition` 如存在，必须位于测试点 children 第一位，且不能包含 `children`。
- `expect` 不能包含 `children`。
- JSON 节点 text 自包含：condition / step / expect 中不出现工单号、附件文件名、来源引用、章节引用、AI 工作过程。

---

## 抗幻觉硬规则

1. 预期里的提示文案，文档没写**绝不编造确切措辞**——用功能性描述（如 `提示校验失败、不可保存`），不纠结、**不标 `[待确认]`**；`[待确认]` 只留给业务行为/规则本身不确定处。
2. 预期里的字段名 / 值必须来自 `requirement.md` / `business-context.md` / `linked-issues.md` / `_kb/`。
3. 预期里的状态变化必须有依据，不要写“应该会变成 XX”。
4. 骨架里的 UI 元素（按钮/弹窗/菜单/提示）用功能性描述承接，不纠结精确文案、不标 `[待确认]`（UI 文案见 `_kb/_global/case-writing-spec.md §5`）。
5. `expect.text` 中的字段名、参数名、选项值、按钮文案统一用中文双引号 `“...”`，不要用 markdown 反引号或英文双引号。
6. 禁止使用 `<b>`、`<strong>`、损坏 span、HTML 实体转义。
7. 若骨架已有客户/正式账套号/租户名/测试环境类 condition（如 `账套=29233 美多商贸`），**必须在聊天报告中标为质量问题**，不要默默补 expect 继续生成——除非 L1 方案明确"按账套/租户生效"。

---

## 完成后聊天报告

自查报告只输出在聊天回复中，不写入 JSON 文件：

```text
test-design.json 已补全：共 N 个测试点，M 个 step，M 个 expect，前置条件 K 个，待确认项 L 个；JSON 校验通过/失败。
```
