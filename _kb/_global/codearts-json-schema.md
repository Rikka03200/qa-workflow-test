# CodeArts 测试设计 JSON 树契约

> 华为云 CodeArts 思维导图式测试设计的 JSON 序列化格式。所有 AI 产出 JSON 必须严格遵守本契约。

---

## 1. 整体结构

测试设计是一棵**单根树**，序列化为 JSON 数组（数组长度始终为 1，里面装根节点）：

```json
[ { 根节点 } ]
```

---

## 2. 字段定义

| 字段 | 类型 | 出现位置 | 含义 |
|---|---|---|---|
| `id` | string | 所有节点 | 32 位大写十六进制 UUID（无连字符），全局唯一 |
| `text` | string | 所有节点 | 节点文本（支持 `<br>` 换行；关键字段/参数/按钮/选项值统一用中文双引号 `“...”`）|
| `side` | string | 根节点 | 固定为 `"right"` |
| `children` | array | 容器节点 | 子节点列表 |
| `mark.priority` | object | 测试点节点 | `{"1": true}` = P1，`{"2": true}` = P2，`{"3": true}` = P3 |
| `testPoint.id` | string | 测试点节点 | 32 位 UUID，标识测试点身份 |
| `testcases` | string[] | 测试点节点（可选）| CodeArts 自动生成的用例编号；新建可省略，CodeArts 落库时生成 |
| `step` | `"Y"` | 步骤节点 | 标识此节点为"步骤"，必须有 1 个 `expect` 子节点 |
| `expect` | `"Y"` | 预期节点 | 标识此节点为"预期"，无子节点 |
| `condition` | `"Y"` | 前置条件节点 | 标识此节点为"前置条件"，无子节点 |

---

## 3. 层级模式（强约束）

```
L1  根节点（工单号，如 "EAR-246155"）                         side="right"
└─ L2...Ln  CodeArts 用例存放目录节点（来自产品 modules.md）       普通容器
    └─ Lk   测试点节点                                             mark.priority + testPoint.id
        ├─ [0..1] 前置条件节点                                     condition="Y"，位于第一位
        ├─ 步骤节点 1                                              step="Y"
        │   └─ 预期节点                                            expect="Y"
        ├─ 步骤节点 2                                              step="Y"
        │   └─ 预期节点                                            expect="Y"
        └─ ...
```

**关键规则**：

- 测试点前面的普通容器节点表示 CodeArts 用例存放文件夹路径，不表示系统菜单或功能模块位置
- 容器路径必须来自 `_kb/projects/<product>/modules.md §2 CodeArts 用例存放目录树`
- 系统菜单 / 操作位置只写进 `step.text`，不要反推为 JSON 容器节点；但步骤正文使用短业务入口路径，不照抄 CodeArts 容器全链路（如写 `wms-出库单`，不写 `wms-标品管理-出库操作-出库单`）
- 测试点标题仍按 `_kb/_global/case-writing-spec.md §1` 使用 `{平台}_{模块路径}_{测试要点}`，不要因为容器路径变化而改成文件夹名堆叠
- 前置条件节点（如有）位于测试点的**第一个**子节点
- 每个 `step` 节点**恰好** 1 个 `expect` 子节点
- 步骤之间是**串行**关系，按数组顺序执行
- 测试点节点的 `text` 即用例标题

---

## 4. ID 生成

- 32 位大写十六进制无连字符
- 同一文件内不可重复
- AI 生成时用伪随机即可，**长度必须严格 32 位**

JavaScript 参考：
```js
crypto.randomUUID().replace(/-/g, '').toUpperCase()
```

Python 参考：
```python
import uuid; uuid.uuid4().hex.upper()
```

---

## 5. HTML 支持（实测约束）

CodeArts 思维导图 text 渲染行为（基于公司 CodeArts 实例 2026-05 实测）：

| 标签 | 实测行为 | 规范 |
|---|---|---|
| `<br>` | **渲染换行** | **允许使用**——多条前置/多条预期换行优先用 `<br>` |
| `<b>...</b>`、`<strong>...</strong>` | **不渲染，按字面显示** | **禁止使用**——会让"加粗"标签原样进入用例正文 |
| `&lt;`、`&gt;`、`&amp;` 等实体 | 按字面显示，不解码 | 禁止——正文引号写中文双引号 `“...”`，尖括号直接写 `<`/`>` |
| `《span style=...》...《/spanspan》` | 历史污染样本，按字面显示 | 严禁——见历史用例中需要清洗的反例 |

**强调的替代写法**（当 `<b>` 不可用时）：
- 直接让上下文表达："采购量=30"已经清晰，不必加粗
- 用中文双引号：`“true”`、`“按分组”`、`“先进先出”`
- **不要用 markdown 反引号** `` `...` `` 包裹业务字段、参数、按钮、选项值；CodeArts 正文应直接显示中文双引号 `“...”`
- 用换行 + 编号让结构本身突出，而非依赖加粗

需要引号时写中文双引号 `“...”`；只有 JSON 语法本身的键和值外层使用英文双引号。需要单引号写 `'`，**不做 HTML 实体转义**。

---

## 6. 空白骨架（复制即用）

```json
[
  {
    "id": "REPLACE_WITH_32CHAR_UUID_A1",
    "text": "EAR-XXXXXX",
    "side": "right",
    "children": [
      {
        "id": "REPLACE_WITH_32CHAR_UUID_A2",
        "text": "web",
        "children": [
          {
            "id": "REPLACE_WITH_32CHAR_UUID_A3",
            "text": "设置管理",
            "children": [
              {
                "id": "REPLACE_WITH_32CHAR_UUID_A4",
                "text": "基础设置",
                "children": [
                  {
                    "id": "REPLACE_WITH_32CHAR_UUID_A5",
                    "text": "系统参数",
                    "children": [
                      {
                        "id": "REPLACE_WITH_32CHAR_UUID_TP1",
                        "text": "web_配送客户代码规则_参数配置_增加“配送客户代码规则”参数",
                        "mark": { "priority": { "2": true } },
                        "testPoint": { "id": "REPLACE_WITH_32CHAR_UUID_TPID1" },
                        "children": [
                          {
                            "id": "REPLACE_WITH_32CHAR_UUID_COND1",
                            "text": "1.前置条件1<br>2.前置条件2",
                            "condition": "Y"
                          },
                          {
                            "id": "REPLACE_WITH_32CHAR_UUID_STEP1",
                            "text": "页面路径，动作描述",
                            "step": "Y",
                            "children": [
                              {
                                "id": "REPLACE_WITH_32CHAR_UUID_EXP1",
                                "text": "可校验的预期结果",
                                "expect": "Y"
                              }
                            ]
                          }
                        ]
                      }
                    ]
                  }
                ]
              }
            ]
          }
        ]
      }
    ]
  }
]
```

---

## 7. 优质样本

### 样本 1：列表删除校验（P2，3 步）

```json
{
  "id": "1937FE90F60549FF92CDD120F0F4E6A9",
  "text": "web_批次推荐规则_列表_删除校验",
  "mark": { "priority": { "2": true } },
  "testPoint": { "id": "2C6D8226AD0048318B01E108C56B5144" },
  "children": [
    { "id": "...COND",  "text": "规则1，未绑定商品；规则2，已绑定商品A", "condition": "Y" },
    { "id": "...STEP1", "text": "wms-批次推荐规则，未勾选规则", "step": "Y",
      "children": [{ "id":"...EXP1", "text":"不允许点击“删除”", "expect":"Y" }] },
    { "id": "...STEP2", "text": "选择规则1、规则2，点击删除", "step": "Y",
      "children": [{ "id":"...EXP2", "text":"显示“批量删除”弹窗", "expect":"Y" }] },
    { "id": "...STEP3", "text": "点击开始执行", "step": "Y",
      "children": [{ "id":"...EXP3", "text":"逐一删除规则1、规则2，商品A自动解绑规则2", "expect":"Y" }] }
  ]
}
```

学习点：
- 三段式标题
- 前置条件简洁交代规则的绑定关系
- 步骤覆盖 3 个分支：未勾选→校验、勾选删除→弹窗、确认删除→联动解绑
- 预期具体到按钮文案与联动效果

### 样本 2：仓配 App 拣货优先级（P2，多步多预期）

详见 `_kb/projects/wms/case-samples/historical-cases.json`（多客户单/多库位系列）。
每条用例：
- 前置条件含 4-5 个编号项（参数、库位、分组、波次策略、出库订单）
- 步骤是"领取→预期→取消领取→预期"反复
- 预期均包含 `分拣库位=X-XX，生产日期=YYYY-MM-DD，批次可拣量=N公斤`

---

## 8. 输出后自查清单

每次产出 JSON 后逐项自查：

- [ ] 根节点 `text` 是工单号（`EAR-xxxxxx`），有 `side: "right"`
- [ ] 测试点前的普通容器路径来自产品级 `modules.md §2 CodeArts 用例存放目录树`
- [ ] 所有 `id` 是 32 位大写十六进制，且全局唯一
- [ ] 每个测试点有 `mark.priority` 和 `testPoint.id`
- [ ] 每个 `step` 节点恰好 1 个 `expect` 子节点
- [ ] 前置条件节点（如有）位于测试点的第一个子节点
- [ ] 测试点标题符合 `_global/case-writing-spec.md §1` 命名规则
- [ ] 步骤包含短业务入口路径 + 动作（`case-writing-spec.md §4`），不照抄 CodeArts 用例存放目录全链路；预期可校验（§5）
- [ ] **无 `<b>` / `<strong>` 等加粗标签**（CodeArts 不渲染，按字面显示）
- [ ] **无 markdown 反引号**包裹业务字段、参数、按钮、选项值；此类内容统一使用中文双引号 `“...”`
- [ ] 无 `《span...》` 等损坏 HTML
- [ ] **测试点标题不含**"场景 N"、"EAR-xxx 回归"、"bug-xxx"等无信息量占位（见 `case-writing-spec.md §1` 标题硬约束）
- [ ] **JSON 节点 text 完全自包含**：condition / step / expect 节点的 text 中**不出现**工单号（`EAR-xxxxxx`）、附件文件名（`screenshot-x.png` / `image-xxx.png`）、外部文档引用（`[来源: ...]` / `requirement.md` / `§修改方案`）、AI 过程信息——这些是元数据，只放产物 markdown，不进 JSON
- [ ] JSON 合法（用 jsonlint.com 或 `python -m json.tool` 验证）
- [ ] 末尾附"待确认问题清单"（如有）
