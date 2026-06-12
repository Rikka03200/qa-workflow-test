# /qa:spot-check — 结构化对抗式强抽检

> 用法：`/qa:spot-check EAR-240883`　或　`/qa:spot-check EAR-1,EAR-2`（多单）
> 时机：弱模型批量生成 + 过校验闸门**之后**，由强模型（你=主 Claude）对每单做严格质检。
> 定位：校验器兜结构，本步兜「机器查不出、需判断」的质量——覆盖/算术/语义/越界/编造。

## 你（主 Claude）要做的

1. 解析参数里的工单号；默认 `product=wms`（用户可指定）。在 `tickets/<product>/` 下定位每个工单目录，转成**绝对路径、正斜杠**形式（如 `D:/Projects/qa-workflow/tickets/wms/2026-05-12/EAR-240883`）。

2. 调用 **Workflow 工具**运行强抽检（脚本已就绪，勿重写）：
   ```
   Workflow({
     scriptPath: "D:/Projects/qa-workflow/.claude/workflows/qa-spot-check.js",
     args: { ticketDirs: ["<每个工单的绝对正斜杠路径>"], product: "wms" }
   })
   ```
   它对每单按 5 维（覆盖完整性 / 算术与不变量 / L1L2L3 与方案语义 / 越界与范围 / 待确认诚信）**并行审查**，算术维度用 python **独立复算**，每条发现再**对抗式核验**剔除误报。

3. 工作流返回每单的 `confirmed`（已核实问题数组，含 `dimension/severity_adjusted/test_point/problem/evidence/suggested_fix`）。对每单把结果写成 `<工单目录>/_spot-check.md`：标题 + 按严重度（高→低）列出确认问题（维度·测试点·问题·证据·建议）；无确认问题则写「本单强抽检未发现确认问题（覆盖/算术/语义/越界/诚信 5 维均通过）」。

4. 给用户一句话汇总：每单确认问题数 + 最严重的几条；询问是否按建议修正（可让弱模型据反馈 `batch_generate --force` 重生成，或你直接改 JSON）。

## 注意
- 这是**强模型**质检：花主 Claude 额度、**不动百炼**。
- 不重复结构类检查（UUID/格式/容器/自包含已由 `validate-test-design.py` / `validate-containers.py` / `check-ticket-artifacts.py` 兜底）。
- CodeArts JSON 根节点 `text=EAR-xxxxxx` 是合法契约；不要把根节点工单号判为自包含问题。工单号禁入只审测试点标题、condition/step/expect 等执行正文。
- 专项检查“账套/客户/环境硬编码前置”：审 condition/step/expect 是否含 `账套=数字`、客户名、租户名、`登录某客户账套`、`在 XX 环境测试` 等。定制客户/单账套工单在测试环境按普通用例对待，不写正式账套号；唯一例外是 L1 方案明确“按账套/租户生效/灰度/隔离/可见性差异”（如“仅针对 4301_博全开放”）。
- 确认问题修复、且该单经你评审通过后，跑 `python scripts/promote_gold.py <工单目录> --note "..."` 把它提升为**黄金范例**，喂回弱模型形成正反馈。
