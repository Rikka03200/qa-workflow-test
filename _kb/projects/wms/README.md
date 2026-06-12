# WMS 产品线（含仓配 App）

## 概览

仓储管理系统（WMS Web）+ 移动端拣货（仓配 App）。覆盖标准管理、库位管理、商品档案、出库订单、波次计划、拣货、容器拆并、分拣等业务域。

## 平台标识（用于测试点命名前缀）

| 平台 | 标识 | 范围 |
|---|---|---|
| WMS Web | `web` | PC 端管理后台 |
| 仓配 App | `仓配app` | 移动端拣货 / 收货 / 复核 等 |
| 采配 App | `采配app` | 直配采购录入 / 采购任务 等 |
| 零售 App | `零售app` | 乐檬零售对接 |
| POS | `POS` | POS 端（**大写**）|
| TMS | `TMS` | 司机小程序（**大写**，容器节点亦可写 `TMS小程序`）|
| 供应商 App | `供应商app` | 供应商平台移动端 |
| 前置仓 App | `前置仓app` | 前置仓配套 |
| PDA | `PDA` | 老 PDA 端 |

> **大小写硬规则**：`POS` / `TMS` / `PDA` 全大写；其余全小写。详见 `modules.md §1`。

## 关联系统

| 系统 | Jira project | Confluence space |
|---|---|---|
| Jira | `EAR` | — |
| Confluence | — | `WMS`、`PRD` |

> 实际值以 `config/config.local.yaml` 的 `products.wms` 为准。

## 知识库文件

| 文件 | 内容 |
|---|---|
| [`modules.md`](./modules.md) | 模块树、菜单路径、命名约定 |
| [`rules.md`](./rules.md) | 业务规则全集（按模块章节）|
| [`terms.md`](./terms.md) | 业务术语、值域、同义词 |
| [`case-samples/`](./case-samples/) | 历史优质用例、风格注解、反例 |

## 写用例时的加载顺序

1. `_kb/_global/case-writing-spec.md` → 通用写法
2. `_kb/_global/codearts-json-schema.md` → JSON 契约
3. 本目录 `terms.md` → 术语
4. 本目录 `modules.md` → 模块路径
5. 本目录 `rules.md` → 业务规则
6. 当前工单 `requirement.md` + `business-context.md`
7. 必要时通过 MCP 拉 Jira/Confluence 补充

## 变更治理

- 新业务规则必须经评审后由 `/qa:kb-extract` 提议入库
- 与历史规则冲突时，**以本次需求为准**，原规则在 `rules.md` 章节末加 `> 变更记录：YYYY-MM-DD 由 EAR-xxxx 更新`
