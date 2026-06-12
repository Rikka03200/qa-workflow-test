# /qa:skeleton — 用例骨架（JSON 树，仅到步骤标题）

> 用法：`/qa:skeleton`
> 前置：`/qa:points` 输出已经过你审增删

---

## AI 任务

基于已确认的测试点列表，生成测试设计 JSON 树**骨架**，严格按 `_kb/_global/codearts-json-schema.md` 输出。

### 骨架的范围

- 完整层级：工单号 → CodeArts 用例存放目录节点（来自 `_kb/projects/<product>/modules.md §2`）→ 测试点 → (前置条件? + 步骤节点们)
- 测试点前面的普通容器节点只表示 CodeArts 用例存放文件夹路径，不表示系统菜单或功能模块位置
- 步骤 `text` 使用业务入口短路径，不写 CodeArts 存放目录全链路；WMS web 优先写 `wms-{页面}` 或 `wms-{页面}-{列表/新增/编辑/明细/tab}`
- 系统菜单 / 操作位置只写进 `step.text`，不要反推为 JSON 容器节点
- 每个测试点节点包含：
  - `id`（32 位大写十六进制 UUID）
  - `text`（按 §1 命名）
  - `mark.priority`
  - `testPoint.id`（32 位 UUID）
- 步骤节点：
  - `id`、`text`、`step: "Y"`
  - **完整列出**所有步骤的 text（含动作位置 + 动作描述）
  - **暂不**生成 expect 子节点（占位 `"children": []`）
- 前置条件节点（如有）：
  - `id`、`text`、`condition: "Y"`
  - **完整写出**——这是最容易出错的部分，先确认
  - 禁止包含 `children` 字段

---

## 输出

合法纯 JSON（可被 `JSON.parse` 解析），写入文件时不要包含 Markdown 代码块围栏或自查报告：

```
tickets/<product>/<sprint-date>/<ticket>/test-design.json
```

格式样例：

```json
[
  {
    "id": "<32位UUID>",
    "text": "EAR-246155",
    "side": "right",
    "children": [
      {
        "id": "<32位UUID>",
        "text": "web",
        "children": [
          {
            "id": "<32位UUID>",
            "text": "设置管理",
            "children": [
              {
                "id": "<32位UUID>",
                "text": "基础设置",
                "children": [
                  {
                    "id": "<32位UUID>",
                    "text": "系统参数",
                    "children": [
                      {
                        "id": "<32位UUID>",
                        "text": "web_配送客户代码规则_参数配置_增加“配送客户代码规则”参数",
                        "mark": { "priority": { "2": true } },
                        "testPoint": { "id": "<32位UUID>" },
                        "children": [
                          {
                            "id": "<32位UUID>",
                            "text": "1.已进入 WMS 系统参数页；<br>2.存在可维护配送客户代码规则的参数区域",
                            "condition": "Y"
                          },
                          {
                            "id": "<32位UUID>",
                            "text": "wms-系统参数，查看“配送客户代码规则”参数",
                            "step": "Y",
                            "children": []
                          }
                        ]
                      },
                      ...其他测试点
                    ]
                  }
                ]
              }
            ]
          }
        ]
      },
      {
        "id": "<32位UUID>",
        "text": "仓配app",
        "children": [...]
      }
    ]
  }
]
```

---

## 抗幻觉硬规则

1. 步骤 text **不要编造** UI 文案/字段位置——文档没写就用功能性描述（如"点击删除按钮"），**不纠结精确文案、不标 `[待确认]`**（UI/提示文案见 `_kb/_global/case-writing-spec.md §5`）；`[待确认]` 只用于业务行为/规则本身不确定处
2. 前置条件**只引用已确认的业务规则**和已知数据形态
3. 所有 ID 必须 32 位大写十六进制，全文件唯一
4. `text` 中的字段名/参数名/选项值/按钮文案统一用中文双引号 `“...”`，不要用 markdown 反引号或英文双引号。
5. 测试点前的普通容器路径必须来自 `_kb/projects/<product>/modules.md §2 CodeArts 用例存放目录树`，不要把系统菜单、tab、规则名、按钮名自创成 JSON 容器节点。
6. 步骤 `text` 不要照抄 CodeArts 容器路径全链路；使用短业务入口路径，例如 `wms-出库单`、`wms-出库单-明细`。
7. `test-design.json` 文件内只能是 JSON；完成报告只输出在聊天回复中。
8. **前置条件禁止写正式环境账套号 / 客户名 / 租户名 / 测试环境标识**（如 `账套=29233 美多商贸`、`登录某客户账套`、`在 XX 环境测试`）。定制客户/单账套工单（如美多物流绩效专题）在测试环境里**按普通用例对待**——前置只写可执行的业务数据条件（如"存在美多物流绩效人员明细数据"）。**唯一例外**：L1 修改方案明确"按账套/租户生效、灰度、隔离、可见性差异"时（如"仅针对'4301_博全'开放"），账套/租户名作为业务规则才可写入。

---

## 完成后报告

```
✓ 骨架已写入 tickets/<product>/<sprint-date>/<ticket>/test-design.json
  - N 个测试点
  - M 个 step 节点（待补 expect）
  - JSON 校验：合法

请确认骨架（特别是前置条件能否在测试环境构造）→ /qa:detail
```
