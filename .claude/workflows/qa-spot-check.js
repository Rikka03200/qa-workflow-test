export const meta = {
  name: 'qa-spot-check',
  description: '对生成的 test-design.json 做结构化对抗式强抽检（覆盖/算术/语义/越界/待确认诚信），含算术独立复算',
  phases: [
    { title: 'Check', detail: '每张用例树按 5 个质量维度并行审查（算术维度用 python 独立复算）' },
    { title: 'Verify', detail: '对抗式核验每条发现，剔除误报' },
  ],
}

const DIMS = [
  { key: 'coverage', label: '覆盖完整性',
    focus: '对照 analysis.md §2「方案切面清单」逐条核对 test-design.json 的测试点：①每条 L1 切面是否都有对应测试点（漏覆盖）；②是否有方案没提的维度被测了（多覆盖：权限/导出/排序/修改记录/必填-长度-字符集 等）；③关联的已解决 bug 是否补了回归用例（见 linked-issues.md / requirement.md）。给出漏/多的具体切面编号 + 测试点标题。' },
  { key: 'arithmetic', label: '算术与不变量',
    focus: '对每个含数值的 expect（个数、数量、金额、取整、分配、累计、阈值、边界），从 condition 给出的输入 + requirement.md 方案公式【用 Bash 跑 python 独立重算】，再与 expect 写的值交叉核对；并检查不变量：累计≤总量、<1 按 1、取整方向、边界端点含/不含。给出「输入→你算的值→用例写的值→是否一致」。本工单若无数值计算，返回空 findings。' },
  { key: 'semantic', label: 'L1/L2/L3 与方案语义',
    focus: '①expect/step 是否混入 L2/L3 来源内容（只允许 L1=方案/规则原文进 expect）；②方案语义是否被简化/类比/自动补全（“≥”写成“=”、比较主语被换、时序当成新动作）——对照 requirement.md §修改方案 与 business-context.md 溯源的 rules.md §，必要时 grep rules.md 对应 § 逐字核。' },
  { key: 'scope', label: '越界与范围',
    focus: '是否有测试点超出本工单方案范围：后续迭代维度、相邻功能、跨工单（web/app 拆单）混入；逐条对照 analysis.md「不覆盖范围」与 test-points.md「不覆盖」。' },
  { key: 'integrity', label: '待确认诚信与自包含',
    focus: '①方案其实没定的点是否被写成确定值（偷偷编造）；②方案已明确的点是否反而标了 [待确认]（过度保守）——对照 questions.md 的已确认答案；③测试点标题、condition/step/expect 等执行正文是否自包含（混入工单号/§/[来源:]/截图名）；④condition/step 是否写了正式账套号/客户名/租户名/测试环境标识（如"账套=29233 美多商贸"、"登录某客户账套"、"在 XX 环境测试"）——定制客户/单账套工单在测试环境按普通用例对待，不写账套号，除非 L1 方案明确"按账套/租户生效/灰度/隔离/可见性差异"（此时才可写，如"仅针对 4301_博全开放"）。注意：CodeArts JSON 根节点 text 按契约就是工单号，根节点出现 EAR-xxxxxx 不作为问题。' },
]

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          dimension: { type: 'string' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          test_point: { type: 'string', description: '涉及的测试点标题或切面编号' },
          problem: { type: 'string' },
          evidence: { type: 'string', description: '原文 / 算式 / § 引用等具体证据' },
          suggested_fix: { type: 'string' },
        },
        required: ['dimension', 'severity', 'test_point', 'problem', 'evidence', 'suggested_fix'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    is_real: { type: 'boolean' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    reasoning: { type: 'string' },
    severity_adjusted: { type: 'string', enum: ['high', 'medium', 'low', 'not-a-bug'] },
  },
  required: ['is_real', 'confidence', 'reasoning', 'severity_adjusted'],
}

const tickets = (args && args.ticketDirs) || []
const product = (args && args.product) || 'wms'
if (!tickets.length) { log('未提供 ticketDirs，无可抽检工单'); return [] }

const ear = d => d.split('/').filter(Boolean).pop()

const rootTicketFalsePositive = f => {
  const text = [f.dimension, f.test_point, f.problem, f.evidence, f.suggested_fix]
    .filter(Boolean)
    .join('\n')
  return /根节点|root node|CodeArts 容器节点|全局容器/.test(text)
    && /工单号|EAR-\d+|EAR-xxxxxx|ticket key/.test(text)
}

const results = await pipeline(
  tickets,
  // Stage 1：5 维并行检查
  dir => {
    const k = ear(dir)
    const arts = ['test-design.json', 'analysis.md', 'requirement.md', 'business-context.md', 'questions.md', 'test-points.md', 'linked-issues.md']
      .map(f => `${dir}/${f}`)
    return parallel(DIMS.map(d => () =>
      agent(
        `你是资深 QA 评审，对工单 ${k} 的 test-design.json 做【${d.label}】维度的严格审查。\n\n` +
        `读取这些文件（绝对路径，缺失则忽略）：\n` + arts.map(a => '- ' + a).join('\n') + '\n' +
        `业务规则真源在 D:/Projects/qa-workflow/_kb/projects/${product}/rules.md（文件大，按需 grep 相关 §，勿全读）。\n\n` +
        `审查重点：${d.focus}\n\n` +
        (d.key === 'arithmetic' ? '必须用 Bash 跑 python 独立重算，附算式与对比。\n' : '') +
        `只报真问题，定位到具体测试点/切面并给证据。没问题就返回空 findings，绝不凑数。`,
        { label: `${k}:${d.key}`, phase: 'Check', schema: FINDINGS_SCHEMA }
      ).then(r => ({ ear: k, dim: d.key, findings: (r && r.findings) || [] }))
    ))
  },
  // Stage 2：对抗式核验每条发现
  (checks, dir) => {
    const k = ear(dir)
    const all = (checks || [])
      .filter(Boolean)
      .flatMap(c => (c.findings || []).map(f => ({ ...f, ear: k })))
      .filter(f => !rootTicketFalsePositive(f))
    if (!all.length) return []
    return parallel(all.map(f => () =>
      agent(
        `对抗式核验工单 ${f.ear} 的一条强抽检发现——尝试【反驳】它，别轻易采信。\n\n` +
        `维度：${f.dimension}\n严重度(初判)：${f.severity}\n测试点：${f.test_point}\n问题：${f.problem}\n证据：${f.evidence}\n建议：${f.suggested_fix}\n\n` +
        `打开 ${dir}/test-design.json 及相关工件（analysis.md / requirement.md / questions.md，必要时 grep rules.md），核到原文。` +
        `若涉算术，用 Bash 跑 python 自己再算一遍。判断它是真问题还是误报（用例其实对 / 方案确实那样 / 纯风格）。拿不准默认 is_real=false。`,
        { label: `verify:${f.ear}:${(f.test_point || '').slice(0, 18)}`, phase: 'Verify', schema: VERDICT_SCHEMA }
      ).then(v => ({ finding: f, verdict: v }))
    ))
  }
)

const out = []
for (let i = 0; i < tickets.length; i++) {
  const k = ear(tickets[i])
  const verified = (results[i] || []).filter(Boolean)
  const confirmed = verified
    .filter(r => r.verdict && r.verdict.is_real && r.verdict.severity_adjusted !== 'not-a-bug')
    .map(r => ({ ...r.finding, severity_adjusted: r.verdict.severity_adjusted, verify_reason: r.verdict.reasoning }))
  out.push({ ear: k, dir: tickets[i], confirmed, checked: verified.length })
  log(`${k}: 核验 ${verified.length} 条 → 确认 ${confirmed.length} 条真实问题`)
}
return out
