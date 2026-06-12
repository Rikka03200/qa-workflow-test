"""强模型审计的结构化输出 schema（原样取自 .claude/workflows/*.js）。

v1 的 SDK 调用以「指令模型只输出匹配该形状的 JSON」+ 容错解析实现（跨 SDK 版本稳健），
故这里既是契约文档，也用于拼进 prompt。字段语义与 JS 版严格一致。
"""

from __future__ import annotations

# 5 个抽检维度（qa-spot-check.js 的 DIMS）
SPOT_CHECK_DIMS = [
    {"key": "coverage", "label": "覆盖完整性",
     "focus": "对照 analysis.md §2「方案切面清单」逐条核对 test-design.json 的测试点："
              "①每条 L1 切面是否都有对应测试点（漏覆盖）；②是否有方案没提的维度被测了"
              "（多覆盖：权限/导出/排序/修改记录/必填-长度-字符集 等）；③关联已解决 bug 是否补了回归用例。"
              "给出漏/多的具体切面编号 + 测试点标题。"},
    {"key": "arithmetic", "label": "算术与不变量",
     "focus": "对每个含数值的 expect（个数/数量/金额/取整/分配/累计/阈值/边界），从 condition 输入 + "
              "requirement.md 方案公式【独立重算】再与 expect 写的值交叉核对；并检查不变量："
              "累计≤总量、<1 按 1、取整方向、边界端点含/不含。给出「输入→你算的值→用例写的值→是否一致」。"
              "本工单若无数值计算，返回空 findings。"},
    {"key": "semantic", "label": "L1/L2/L3 与方案语义",
     "focus": "①expect/step 是否混入 L2/L3 来源内容（只允许 L1=方案/规则原文进 expect）；"
              "②方案语义是否被简化/类比/自动补全（“≥”写成“=”、比较主语被换、时序当成新动作）"
              "——对照 requirement.md §修改方案 与 business-context.md 溯源的 rules.md § 逐字核。"},
    {"key": "scope", "label": "越界与范围",
     "focus": "是否有测试点超出本工单方案范围：后续迭代维度、相邻功能、跨工单（web/app 拆单）混入；"
              "逐条对照 analysis.md「不覆盖范围」与 test-points.md「不覆盖」。"},
    {"key": "integrity", "label": "待确认诚信与自包含",
     "focus": "①方案没定的点是否被写成确定值（偷偷编造）；②方案已明确的点是否反而标了 [待确认]（过度保守）"
              "——对照 questions.md 已确认答案；③执行正文是否自包含（混入工单号/§/[来源:]/截图名）；"
              "④是否写了正式账套号/客户名/租户名/测试环境标识（除非 L1 方案明确按账套/租户生效/灰度/隔离）。"
              "注意：CodeArts 根节点 text 按契约就是工单号，根节点出现 EAR-xxxxxx 不算问题。"},
]

FINDINGS_SCHEMA = {
    "findings": [
        {"dimension": "str", "severity": "high|medium|low",
         "test_point": "涉及的测试点标题或切面编号", "problem": "str",
         "evidence": "原文/算式/§ 引用等具体证据", "suggested_fix": "str"}
    ]
}

VERDICT_SCHEMA = {
    "is_real": "bool", "confidence": "high|medium|low",
    "reasoning": "str", "severity_adjusted": "high|medium|low|not-a-bug",
}

# qa-resolve.js 的 schema
QUESTION_SCHEMA = {
    "question": "题号+问题陈述（新增漏问可不带题号）",
    "already_answered": "bool（仅既有题：✅答案是否已有人工内容；新增漏问固定 false）",
    "status": "resolved|needs_human",
    "answer": "resolved 时给确定答案；needs_human 留空",
    "source": "答案出处；needs_human 写已检索过哪些来源",
    "reason": "为何能定论/为何仍需人工",
    "problem": "新增漏问：为何这是写确定用例必须知道的业务事实",
    "possible_scenarios": ["新增漏问：至少 2 个可供人工选择的实际业务场景"],
    "impact": "新增漏问：影响哪些步骤/预期/覆盖",
}

RESOLVE_SCHEMA = {"resolutions": [QUESTION_SCHEMA], "missing_questions": [QUESTION_SCHEMA]}
VERIFY_SCHEMA = {"supported": "bool", "confidence": "high|medium|low", "reasoning": "str"}

# 草稿复核（draft_review.py）：对抗式判定一条草稿发现是否【必须人工拍板才能写对用例】，
# 是则渲染成给人工的问题（折进 questions.md）。默认 needs_human=false——纯格式/自包含/算术/
# 可由 L1 方案或证据确定的、UI 文案，都不算；绝不把草稿里的 [待确认] 标记当问题“收割”。
DRAFT_QUESTION_SCHEMA = {
    "needs_human": "bool（写对该用例是否必须人工/产品/原型拍板：方案或业务规则本身不确定。"
                   "纯结构/自包含/算术/可由 L1 方案或既有证据确定的、UI 文案 → false）",
    "confidence": "high|medium|low",
    "question": "needs_human 时给人工的问题（一句话业务语言，禁工单号/§/节点 id/文件名/截图名）",
    "problem": "needs_human 时：为何写确定用例必须先知道这个",
    "possible_scenarios": ["needs_human 时：至少 2 个可供人工选择的实际业务取值/分支"],
    "impact": "needs_human 时：影响哪些用例/预期",
    "source": "needs_human 时：真实可追溯出处：需求方案原句、关联工单 EAR-xxxxxx 的方案/评论原话、业务规则、或已确认待确认答案；禁止写 AI 建议/草稿复核/方案未明确",
    "reasoning": "str",
}

# 复核后修复（revise.py）：强模型只给「改哪个已存在节点、新文本」，Python 按 id 改 text。
# 【铁律】edits 只做有 L1/已确认答案依据的纯文字修正，**绝不在 text 里新增 [待确认]**（确定性护栏也会拦）。
# 需人工拍板的点 → questions（折进 questions.md 可回答），需结构性增删的 → unfixable（交重新生成）。
REVISE_SCHEMA = {
    "edits": [
        {"node_id": "32 位 hex（取自内嵌 test-design.json 的 id 字段，必须是已存在节点）",
         "old_text_snippet": "原 text 片段（便于核对定位，可空）",
         "new_text": "修正后的完整 text（有据的纯文字修正；换行只用 <br>、自包含、"
                     "禁工单号/§/[来源:]/文件名/截图名、**禁新增 [待确认]**）",
         "reason": "依据哪条复核发现修这一处（须有 L1/已确认答案支撑）"}
    ],
    "questions": [
        {"question": "需人工/产品拍板才能写对用例的问题（业务语言，一句话；如"
                     "‘本工单无明确修改方案，请确认要测的 L1 范围/规则’、‘A 与 B 同时配置时取交集还是分别过滤’）",
         "problem": "为何写确定用例必须先知道这个",
         "source": "真实可追溯出处：需求方案原句、关联工单 EAR-xxxxxx 的方案/评论原话、业务规则、或已确认待确认答案；禁止写 AI 建议/复核发现/方案未明确",
         "possible_scenarios": ["至少 2 个可供人工选择的实际业务取值/分支"],
         "impact": "影响哪些用例/预期"}
    ],
    "unfixable": [
        {"finding": "需结构性改动（漏覆盖切面/越界需删整条等）的发现摘要",
         "why": "为何仅改文本无法修复，需重新生成或人工补"}
    ],
}

REVISE_VERIFY_SCHEMA = {
    "resolved": "bool（所有 _spot-check.md 已确认发现是否都已被消解或正确分流）",
    "leftover": [
        {"finding": "仍未消解的复核发现摘要",
         "fix_type": "text|question|structural|unknown（纯文本可修 / 需人工确认 / 需结构新增删除 / 无法判断）",
         "node_id": "fix_type=text 时：仍需修的已存在节点 id；否则可空",
         "current_text": "当前仍有问题的节点 text 或相关摘要",
         "suggested_new_text": "fix_type=text 时：可直接替换的完整新 text；否则留空",
         "source": "真实可追溯出处：需求方案原句、关联工单评论、业务规则或已确认待确认答案；禁止写 AI 建议/复核发现/方案未明确",
         "why": "为何仍未消解，以及为何属于该 fix_type"}
    ],
    "reasoning": "验收结论摘要",
}

# 结构修复（repair.py）：弱模型连试多轮仍未过结构校验时的强模型兜底——整树重写、只修结构不改业务，
# 仍由 validate-test-design + 测试点数不减 + .bak + 回滚兜底。只动 json_ok==False（结构坏）的单。
REPAIR_SCHEMA = {
    "test_design": "完整、合法的 test-design.json（JSON 数组，单根树）：修正全部结构 FAIL，"
                   "保持测试点/步骤/预期的业务含义与数量不减，不借修结构删用例或改业务判定",
}
