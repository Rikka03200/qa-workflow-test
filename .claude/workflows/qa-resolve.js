export const meta = {
  name: 'qa-resolve',
  description: '强模型证据消解：人工回答前做证据完整性审查，补充漏问并自动答掉有据问题，只把真无据的留给人工',
  phases: [
    { title: 'Resolve', detail: '每单基于实际证据审查 questions.md 是否漏问，并逐条试答待确认点' },
    { title: 'Verify', detail: '对抗式核验每条“已据消解”，证据不足则降级为需人工' },
    { title: 'Apply', detail: '把补充问题与经核验答案写回 questions.md（只填空、不碰人工已填），写 _resolve.md' },
  ],
}

const QUESTION_SCHEMA = {
  type: 'object',
  properties: {
    question: { type: 'string', description: '题号+问题陈述，如 Q1: 切换查询单位后表头是否变化；新增漏问可不带题号' },
    already_answered: { type: 'boolean', description: '仅既有 questions.md 题目使用：✅答案 是否已有人工非注释内容；新增漏问固定 false' },
    status: { type: 'string', enum: ['resolved', 'needs_human'], description: 'resolved=证据能定论；needs_human=穷尽证据仍无据/需原型/需产品' },
    answer: { type: 'string', description: 'status=resolved 时给出确定答案；needs_human 时留空' },
    source: { type: 'string', description: '答案出处；needs_human 时写已检索过哪些来源' },
    reason: { type: 'string', description: '为何能定论或为何仍需人工（需原型图/需产品决策）' },
    problem: { type: 'string', description: '新增漏问使用：为什么这是写成确定用例必须知道的实际业务事实；既有题可留空' },
    possible_scenarios: { type: 'array', items: { type: 'string' }, description: '新增漏问使用：可供人工选择/填写的实际业务场景；已据消解题可为空数组' },
    impact: { type: 'string', description: '新增漏问使用：该事实影响哪些测试步骤/预期/覆盖范围；既有题可留空' },
  },
  required: ['question', 'already_answered', 'status', 'answer', 'source', 'reason', 'problem', 'possible_scenarios', 'impact'],
}

const RESOLVE_SCHEMA = {
  type: 'object',
  properties: {
    resolutions: {
      type: 'array',
      items: QUESTION_SCHEMA,
      description: 'questions.md 中既有问题的处理结果',
    },
    missing_questions: {
      type: 'array',
      items: QUESTION_SCHEMA,
      description: '证据完整性审查发现的漏问：仅限人工回答前、基于实际证据发现、非 test-design.json 收割',
    },
  },
  required: ['resolutions', 'missing_questions'],
}

const VERIFY_SCHEMA = {
  type: 'object',
  properties: {
    supported: { type: 'boolean', description: '证据是否确实支持该答案（尝试反驳后仍成立）' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    reasoning: { type: 'string' },
  },
  required: ['supported', 'confidence', 'reasoning'],
}

const APPLY_SCHEMA = {
  type: 'object',
  properties: {
    ear: { type: 'string' },
    added_questions: { type: 'number', description: '实际追加到 questions.md 的漏问题数（含自动消解与需人工）' },
    resolved_applied: { type: 'number', description: '实际写回 questions.md 的自动消解答案数（既有题+新增题）' },
    needs_human: { type: 'number', description: '仍留给人工的题数' },
    skipped_human_answered: { type: 'number', description: '因人工已填而跳过未动的题数' },
    notes: { type: 'string' },
  },
  required: ['ear', 'added_questions', 'resolved_applied', 'needs_human', 'skipped_human_answered', 'notes'],
}

const tickets = (args && args.ticketDirs) || []
const product = (args && args.product) || 'wms'
if (!tickets.length) { log('未提供 ticketDirs，无可消解工单'); return [] }

const ear = d => d.split('/').filter(Boolean).pop()
const rulesPath = `D:/Projects/qa-workflow/_kb/projects/${product}/rules.md`

const results = await pipeline(
  tickets,
  // Stage 1：基于实际证据处理既有问题，并审查 questions.md 是否漏问
  dir => {
    const k = ear(dir)
    return agent(
      `你是资深 QA。对工单 ${k} 做【证据消解 + 证据完整性审查】。目标是在人工回答前，把有据可查的问题自动答掉，并检查 questions.md 是否漏掉“写成确定测试用例所必需、但当前证据不足”的业务事实；只把真无据的问题留给人工。\n\n` +
      `重要边界（必须遵守）：\n` +
      `- 只基于实际证据，不做业务“预演”、不模拟系统行为、不按经验猜测。\n` +
      `- 禁止读取或利用 ${dir}/test-design.json、${dir}/test-points.md、${dir}/_spot-check.md 作为漏问来源；本步骤发生在人工回答前，不做 JSON 待确认收割/回填。\n` +
      `- 不补方案外功能；不把 L2/L3 背景、客户报障、开发实现细节当 L1 确定规则。\n` +
      `- 已有人工答案为终裁，绝不修改、绝不反驳，只标 already_answered=true 跳过。\n\n` +
      `读取（绝对路径，缺失忽略）：\n` +
      `- ${dir}/questions.md（待确认表单——本步对象）\n` +
      `- ${dir}/requirement.md（§3 修改方案=L1 规则真源）\n` +
      `- ${dir}/analysis.md（§4 待确认清单 / §2 方案切面）\n` +
      `- ${dir}/linked-issues.md（关联工单描述+评论）\n` +
      `- ${dir}/business-context.md（§3 规则摘录 / §5 Jira 搜索记录）\n` +
      `- ${dir}/_jira-search.md（命中工单全文）\n` +
      `业务规则真源在 ${rulesPath}（文件大，按问题关键词 grep 相关 §，勿全读）。\n\n` +
      `任务 A：逐条处理 questions.md 的每个 ## Q（输出到 resolutions）：\n` +
      `1. 先看其 ✅答案 是否已有人工非注释内容——若有，already_answered=true、status=resolved、answer 填用户已写内容（不要改动，人工答案为终裁），不再消解。\n` +
      `2. 否则用实际证据试答：L1 方案 / 关联工单评论（产品作者方案确认为 L1，其余 L2/L3 仅辅证）/ rules.md 既有规则 / _jira-search 命中。历史先例只有在规则文本明确同一机制/同一模块且不与本次方案冲突时才可辅助，不能单独作为定论。只要证据能定论，status=resolved，answer 给确定答案，source 标出处。\n` +
      `3. 只有穷尽上述证据仍无据、或答案依赖需登录原型图(mastergo/lanhu)、或必须产品拍板的业务决策，才 status=needs_human，source 写“已检索：<列来源/§/JQL> 均无据”，reason 说明为何只能问人工。\n\n` +
      `任务 B：做【证据完整性审查】（输出到 missing_questions）：\n` +
      `逐条检查 requirement.md §3 L1 修改方案、analysis.md §2 方案切面、business-context.md/rules 摘录与 linked-issues/_jira-search 证据，判断当前 questions.md 是否漏掉了“写成确定测试步骤/预期所必需的实际业务事实”。\n` +
      `只有同时满足以下条件，才允许新增漏问：\n` +
      `- 它直接来自 L1 修改方案的覆盖切面，或是写该 L1 切面用例时必需确认的实际业务事实；\n` +
      `- 缺少该事实会导致最终测试步骤/预期无法判断通过或失败标准；\n` +
      `- 当前 questions.md 没有语义相同的问题，analysis.md 已列问题与已被证据消解的问题也未覆盖它；\n` +
      `- 该事实不是 UI/提示文案精确措辞（UI/提示文案一律用功能性描述，不问人工）；\n` +
      `- 该事实不是方案外功能、常规回归、权限/导出/排序/修改记录等方案未提维度；\n` +
      `- 若当前证据已能直接回答，则作为新增且已自动消解的问题留痕；若证据仍不足，才作为新增需人工问题。\n` +
      `新增漏问的 status 也按同一规则处理：证据能定论→resolved 并给 answer/source；证据不足→needs_human 并给 possible_scenarios/impact。\n\n` +
      `铁律：能查到就别留给人工；但绝不为了消解而编造。answer 必须可被 source 逐字/逐条支撑。` ,
      { label: `resolve:${k}`, phase: 'Resolve', schema: RESOLVE_SCHEMA }
    ).then(r => ({
      ear: k,
      dir,
      resolutions: (r && r.resolutions) || [],
      missing_questions: (r && r.missing_questions) || [],
    }))
  },
  // Stage 2：对抗式核验每条“已据消解”（含新增漏问中已据消解项）
  (res, dir) => {
    const k = ear(dir)
    const existing = (res.resolutions || [])
      .filter(x => x.status === 'resolved' && !x.already_answered)
      .map(x => ({ kind: 'existing', item: x }))
    const missing = (res.missing_questions || [])
      .filter(x => x.status === 'resolved')
      .map(x => ({ kind: 'missing', item: x }))
    const toVerify = existing.concat(missing)
    if (!toVerify.length) return { ...res, verified: [] }
    return parallel(toVerify.map(x => () =>
      agent(
        `对抗式核验工单 ${k} 的一条【证据消解】结论——尝试反驳它，别轻易采信。\n\n` +
        `类型：${x.kind === 'missing' ? '证据完整性审查新增问题' : 'questions.md 既有问题'}\n` +
        `问题：${x.item.question}\n拟定答案：${x.item.answer}\n声称出处：${x.item.source}\n理由：${x.item.reason}\n\n` +
        `去 ${dir} 相关工件 + ${rulesPath}（grep 对应 §）核到原文。判断该出处是否确实支撑该答案：\n` +
        `- 出处是否真实存在、且语义确实指向该答案（不是被简化/类比过头/张冠李戴）？\n` +
        `- 是否拿 L2/L3（客户报障/开放提问/被否决方案）当成了 L1 定论？若答案依赖类比、背景、客户期望、截图、未确认讨论、或未实际读取到的来源，supported=false。\n` +
        `- 同机制历史规则是否真的同模块/同机制且无本次方案冲突？\n` +
        `- 若这是新增漏问，确认它不是 UI 文案精确措辞、不是方案外功能、不是从 test-design.json 收割来的问题。\n` +
        `拿不准默认 supported=false（宁可退回人工，也不要自动答错）。`,
        { label: `vrf:${k}:${x.kind}:${(x.item.question || '').slice(0, 12)}`, phase: 'Verify', schema: VERIFY_SCHEMA }
      ).then(v => ({ kind: x.kind, item: x.item, verdict: v }))
    )).then(vs => ({ ...res, verified: vs.filter(Boolean) }))
  },
  // Stage 3：写回 questions.md（只填空、不碰人工已填）+ 写 _resolve.md
  (res, dir) => {
    const k = ear(dir)
    const verifiedMap = {}
    for (const v of (res.verified || [])) verifiedMap[`${v.kind}::${v.item.question}`] = v.verdict

    const finalize = (x, kind) => {
      if (x.already_answered) return { ...x, _kind: kind, _final: 'human' }
      if (x.status === 'resolved') {
        const v = verifiedMap[`${kind}::${x.question}`]
        if (v && v.supported) return { ...x, _kind: kind, _final: 'resolved' }
        return { ...x, _kind: kind, _final: 'needs_human', reason: `消解核验未通过，退回人工：${(v && v.reasoning) || x.reason}` }
      }
      return { ...x, _kind: kind, _final: 'needs_human' }
    }

    const finalExisting = (res.resolutions || []).map(x => finalize(x, 'existing'))
    const finalMissing = (res.missing_questions || []).map(x => finalize(x, 'missing'))
    const final = finalExisting.concat(finalMissing)

    return agent(
      `你是资深 QA。把工单 ${k} 的证据消解与证据完整性审查结果写回 ${dir}/questions.md，并生成 ${dir}/_resolve.md。\n\n` +
      `终态结果（_kind=existing 表示 questions.md 既有题；_kind=missing 表示本次证据完整性审查新增漏问；_final 为终态）：\n${JSON.stringify(final, null, 2)}\n\n` +
      `操作 questions.md：\n` +
      `一、既有题（_kind=existing）：\n` +
      `- _final=human：人工已填，保持原样，绝不改动。\n` +
      `- _final=resolved：仅当该题 ✅答案 当前为空/HTML注释占位时，填入「<answer>（据 <source> 自动消解）」；若已有人工内容则不动并计入 skipped。\n` +
      `- _final=needs_human：✅答案 保持占位；若该题尚无同类「已自动检索无据」提示，则在该题 ✅答案 上方补一行「> 已自动检索无据，需人工/产品确认：<reason 精简>」；若已有同类提示则更新或保持，绝不重复插入。\n\n` +
      `二、新增漏问（_kind=missing）：\n` +
      `- 若 questions.md 已有语义相同的问题，或已存在本分节下同义问题，不要重复追加；只在 _resolve.md 记录“已存在/本次跳过”。重复运行时也不得重复追加同一问题或重复插入“已自动检索无据”提示。\n` +
      `- 否则直接追加到文末，作为连续的 ## QN: ... 题块；不要新增额外二级分节。若原文件正文为“无”，先替换成固定说明行「> 请在每个“✅ 答案”下方填写确认结果；如果暂时无法确认，填写 \`[待确认]\`。」再追加问题。\n` +
      `- 题号接在现有最大 Q 编号之后，必须连续递增。每题格式必须符合 questions.md：\n` +
      `  ## QN: <question>\n  **问题**：<problem/reason 的白话问题>\n  **来源**：<source>\n  **可能场景**：A/B/...（resolved 也可写“已由证据确定：<answer>”）\n  **影响范围**：<impact>\n  **✅ 答案**：\n` +
      `  - _final=resolved：填「<answer>（据 <source> 自动消解）」；\n` +
      `  - _final=needs_human：在答案上方加「> 已自动检索无据，需人工/产品确认：<reason 精简>」，答案保留 <!-- 待填 -->。\n` +
      `- 新增漏问绝不能来自 test-design.json；不要写“来自 JSON 节点/id/回填”。\n\n` +
      `铁律：只动 ✅答案 区、需人工提示行、以及追加的连续 Q 题块；不改人工已填答案；questions.md 是工程产物，可保留来源，但不要使用 <b>/<strong>/HTML 实体。\n\n` +
      `再写 ${dir}/_resolve.md：标题「# 证据消解报告 — ${k}」；分「## 原有问题自动消解」「## 原有问题仍需人工」「## 证据完整性审查新增并自动消解」「## 证据完整性审查新增仍需人工」「## 人工已填（跳过）」五段列出，并记录已检索来源/关键词/章节。\n` +
      `最后返回 ear、added_questions、resolved_applied、needs_human、skipped_human_answered、notes。`,
      { label: `apply:${k}`, phase: 'Apply', schema: APPLY_SCHEMA }
    )
  }
)

const out = []
for (let i = 0; i < tickets.length; i++) {
  const r = results[i]
  if (!r) { out.push({ ear: ear(tickets[i]), dir: tickets[i], error: 'resolve 失败' }); continue }
  out.push(r)
  log(`${r.ear}: 补充问题 ${r.added_questions} 条 / 自动消解 ${r.resolved_applied} 条 / 留人工 ${r.needs_human} 条 / 跳过人工已填 ${r.skipped_human_answered} 条`)
}
return out
