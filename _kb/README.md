# 知识库总览

`_kb/` 是 AI 产出用例的**唯一业务知识来源**（加上当前工单的 `requirement.md` 与 Jira/Confluence 拉取的内容）。
它分两层：

```
_kb/
├── _global/        跨产品规范（用例编写、JSON 契约、QA 方法论）
└── projects/       按产品线分支
    └── <product>/  产品级业务规则、术语、模块图、样本用例
```

## 各文件用途

### `_global/`（跨产品）

| 文件 | 用途 | AI 何时读 |
|---|---|---|
| `case-writing-spec.md` | 测试点命名、步骤动词化、预期可校验等通用写法 | 写用例 |
| `codearts-json-schema.md` | CodeArts 测试设计 JSON 树字段定义、空白骨架、校验清单 | 输出 JSON |
| `qa-methodology.md` | 通用测试设计方法（功能/边界/异常/权限/兼容 维度） | 测试点拆解 |

### `projects/<product>/`（每个产品一份）

| 文件 | 用途 | AI 何时读 |
|---|---|---|
| `README.md` | 产品概览、模块清单、关键术语索引 | 加载产品时 |
| `modules.md` | 模块层级、路径、负责人（可选）| 命名/定位 |
| `rules.md` | 业务规则全集，按模块 + 章节号组织 | 写用例、风险分析 |
| `terms.md` | 业务术语、缩写、同义词、值域 | 遇到陌生术语 |
| `case-samples/` | 历史优质用例样本 + 反例（带注解）| 学风格 |

## 引用规范

AI 写用例时引用 `_kb/` 中规则，**必须**标章节号：

```
[来源: _kb/projects/wms/rules.md §4.2]
[来源: _kb/_global/case-writing-spec.md §3]
[来源: Jira EAR-246155 描述第3段]   ← 实时拉取
[来源: Confluence "WMS 拣货规范" §2.1]  ← 实时拉取
```

## 演进机制

- **新规则进入**：评审后由 `/qa:kb-extract` 提议，人工审通过后写入对应 `rules.md` 章节
- **冲突解决**：本次需求与历史规则冲突时，**以本次需求为准**，并在 `rules.md` 章节末注明 `> 变更记录：YYYY-MM-DD 由 EAR-xxxx 更新，原规则见 archive/`
- **样本沉淀**：评审通过的优质用例 → `case-samples/`；评审打回的 → `case-samples/反例与修正.md`，附原版本 vs 改后版本

## 多产品如何加

```bash
# 1. 在 _kb/projects/ 建新产品目录
mkdir _kb/projects/<new>

# 2. 复制 wms 当模板
cp -r _kb/projects/wms/* _kb/projects/<new>/
# 清空业务内容，保留结构

# 3. 在 config/config.local.yaml 的 products: 区注册
```

## 反模式

- ❌ 把工单临时数据（订单号、客户名、截图）写进 `_kb/`
- ❌ 把"我猜应该是"写进 `rules.md`（要么有原始来源，要么不写）
- ❌ 跨产品共用业务规则（除非确实是跨产品的通用规则，那放 `_global/`）
- ❌ 跳过章节号、不留来源
