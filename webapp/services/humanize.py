"""把 AI 内部引用从“给用户看的文本”里清洗掉（展示层，不改盘上内容）。

弱模型/强模型常在 questions.md 的来源、_spot-check.md 的证据里写
`requirement.md §3`、`rules.md §26.1`、`test-design.json id ...`、`§2.1 第3条`、
`questions.md`、`CLAUDE.md`、32 位 hex 节点 id 等——用户看不懂。本模块把它们映射成
业务词或删除。作为 Jinja 过滤器用于待确认字段与 AI 复核展示。
"""

from __future__ import annotations

import re

# 已知工件文件（可带 §章节/第N条/后缀词）→ 业务词
_FILE_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"_kb/[\w./-]*rules\.md(?:\s*[:：]\s*\d+|\s*§\s*[\d.]+(?:\s*第\s*\d+\s*条)?)?"), "业务规则"),
    (re.compile(r"rules\.md(?:\s*[:：]\s*\d+|\s*§\s*[\d.]+(?:\s*第\s*\d+\s*条)?)?"), "业务规则"),
    (re.compile(r"requirement\.md(?:\s*§\s*[\d.]+)?(?:\s*的)?(?:\s*修改方案)?"), "需求方案"),
    (re.compile(r"business-context\.md(?:\s*§\s*[\d.]+)?"), "业务背景"),
    (re.compile(r"(?:linked-issues|_jira-search|_jira-linked)\.md(?:\s*§\s*[\d.]+)?"), "关联工单"),
    (re.compile(r"analysis\.md(?:\s*§\s*[\d.]+)?"), "需求分析"),
    (re.compile(r"test-points\.md(?:\s*§\s*[\d.]+(?:\s*第\s*\d+\s*条)?)?"), "测试点"),
    (re.compile(r"test-design\.json(?:\s*(?:的)?\s*(?:节点|id)\s*[`]?[0-9A-Fa-f]{6,32}[`]?)?"), "测试用例"),
    (re.compile(r"questions\.md"), "待确认清单"),
    (re.compile(r"(?:CLAUDE|AGENTS)\.md(?:\s*§\s*[\d.]+)?"), "项目规范"),
    (re.compile(r"[\w-]*(?:case-writing-spec|codearts-json-schema|markdown-artifact-schema|style-notes|modules|terms)\.md(?:\s*§\s*[\d.]+)?"), "规范"),
]

# 残留的纯引用/调试碎片 → 删除（顺序：先带关键词的整体，再裸 hex/分级标记）
_RESIDUAL: list[tuple[re.Pattern, str]] = [
    # 节点[ 关键词] <hex>[ text=…]，含外围括号
    (re.compile(r"（?\s*节点\s*(?:step|expect|condition|测试点|预期|步骤|前置)?\s*[`]?[0-9A-Fa-f]{6,32}[`]?(?:\s*text\s*=\s*[^）)\n]*)?\s*）?"), ""),
    # 截断/列表式节点 id（如「节点 ...E112/E114」「...E112/E114/E124」）、行号引用（line 3712 / 第27行）、
    # L 编号列表（L70/L82/L144）、节点 id 括号（（DDDE109））——都是给开发定位用的噪声，对用户无意义，删
    (re.compile(r"节点\s*\.{0,3}\s*[0-9A-Fa-f]{2,}(?:\s*[/、]\s*\.{0,3}[0-9A-Fa-f]{2,})*"), ""),
    (re.compile(r"\.{2,}\s*[0-9A-Fa-f]{2,}(?:\s*[/、]\s*\.{0,3}[0-9A-Fa-f]{2,})*"), ""),
    (re.compile(r"(?<![A-Za-z0-9])L\d+(?:\s*[/、]\s*L?\d+)+"), ""),
    (re.compile(r"\bline\s*\d+(?:\s*[-–]\s*\d+)?", re.I), ""),
    (re.compile(r"第\s*\d+\s*行"), ""),
    (re.compile(r"[（(]\s*(?=[0-9]*[A-F])[0-9A-F]{5,12}\s*[)）]"), ""),
    # step/expect/condition <hex>[ text=…]
    (re.compile(r"\b(?:expect|step|condition)\b\s*[`]?[0-9A-Fa-f]{6,32}[`]?(?:\s*text\s*=\s*[^）)\n]*)?", re.I), ""),
    # id: <hex>
    (re.compile(r"\bid\s*[:：]?\s*[`]?[0-9A-Fa-f]{6,32}[`]?"), ""),
    # 校验调试输出 valid(1)=True 之类
    (re.compile(r"\b\w+\s*\([^)]*\)\s*=\s*(?:True|False|\d+)\b"), ""),
    # 裸 8~32 位大写 hex（节点 id），及裸 32 位
    (re.compile(r"`?[0-9A-F]{8,32}`?"), ""),
    # L1/L2/L3 分级标记
    (re.compile(r"(?<![0-9A-Za-z])L[123](?![0-9A-Za-z])"), ""),
    # 残留 text=
    (re.compile(r"\btext\s*=\s*"), ""),
    # JQL 查询块（反引号包裹，含 project in / ORDER BY / text ~ / component = 等）→ 删
    (re.compile(r"`[^`]*(?:project\s+in|order\s+by|statuscategory|text\s*~|summary\s*~|component\s*=)[^`]*`", re.I), ""),
    # 裸 JQL（无反引号，project in (...) 起头到句末）
    (re.compile(r"project\s+in\s*\([^)]*\)[^。；\n]*", re.I), ""),
    # § 章节：§3 / §3.3 第2条 / §EAR-252821 / §修改方案；最后兜底删掉任何残留的孤立 §
    (re.compile(r"§\s*[A-Za-z]*-?\d+(?:[.\-]\d+)*(?:\s*第\s*\d+\s*条)?"), ""),
    (re.compile(r"§\s*[\d.]+(?:\s*第\s*\d+\s*条)?"), ""),
    (re.compile(r"\s*§\s*"), ""),
    (re.compile(r"\[来源[:：][^\]]*\]"), ""),
    (re.compile(r"_kb/[\w./-]+"), ""),
]

# 清理替换后留下的空括号/悬空斜杠/重复标点/多余空白
_CLEANUP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[（(]\s*[)）]"), ""),
    (re.compile(r"[（(]\s*[，、；/]"), "（"),
    (re.compile(r"[，、；/]\s*[)）]"), "）"),
    (re.compile(r"\s*/\s*/\s*"), " "),            # 连续悬空斜杠
    (re.compile(r"(?:^|\s)/(?=\s|$)"), " "),       # 单个悬空斜杠
    (re.compile(r"[ \t]{2,}"), " "),
    (re.compile(r"(（来自)\s*(自动消解|回填)"), r"\1证据"),
]


def humanize(text: str) -> str:
    if not text:
        return text
    s = str(text)
    for pat, repl in _FILE_MAP:
        s = pat.sub(repl, s)
    for pat, repl in _RESIDUAL:
        s = pat.sub(repl, s)
    for pat, repl in _CLEANUP:
        s = pat.sub(repl, s)
    # 收尾：去重复的业务词分隔、首尾标点
    s = re.sub(r"(业务规则|需求方案|关联工单|测试点|测试用例)(\s*[、，]\s*\1)+", r"\1", s)
    return s.strip(" 、，;；")


# ── 「依据/来源」字段专用清洗：在 humanize 之上，删搜索过程叙述、删悬空标签、按来源分行 ──
# 搜索/检索过程叙述（属于 AI 工作过程、非真实来源，用户不该看到，整段删除）
_PROCESS_HINT = re.compile(
    r"已检索|已搜索|已搜过|已搜(?!索)|已查(?:阅|询|找|证|过)?|未命中|未找到|查无|"
    r"无据可查|无可确认|不适用\s*JQL|JQL|project\s+in|order\s+by|statuscategory|maxresults",
    re.I)
# 剥掉来源标签词后用于判断“是否只剩悬空标签”（如「业务背景 原句」）
_LABEL_WORDS = re.compile(
    r"需求方案|业务背景|关联工单|业务规则|测试点|测试用例|项目规范|需求分析|待确认清单|"
    r"规范|修改方案|解决方案|原句|摘录|评论|[、，,。；;：:\s的]")

# 成对引号计深度——句末标点只在引号外才断行，避免把引号里的句号当成分句、把原句拦腰断开
# （用户反馈“换行没换明白”）。全角引号字符一律用 \u 转义写，源码里不出现字面全角引号。
_Q_OPEN = "「『“‘"    # 「『 + 左双/单弯引号
_Q_CLOSE = "」』”’"   # 」』 + 右双/单弯引号
_BREAK_PUNCT = "。；\n"         # 。 ； 换行
_TRIM_CHARS = " 、，,;；。\t"   # 、，,;；。制表

# L3 客户/背景类来源——不是修改方案/业务规则，当依据没用，整段删（用户明确要求）。
_L3_LEAD = ("本工单客户与背景", "客户与背景", "客户背景", "背景与价值", "客户场景", "期望目标", "背景信息")
_L3_LEAD_RE = re.compile(r"^\s*(?:本工单\s*)?(?:客户与背景|客户背景|背景与价值|客户场景|期望目标|背景信息)")
# 来源引导词：引号外遇到它就另起一行，做到一个来源一行。
# 不含 修改方案/解决方案——它们常出现在某条来源内部（如 关联工单 X 的修改方案摘录），
# 列为引导词会把同一条来源拆断。
_SOURCE_LEAD = ("需求方案", "业务规则", "业务背景", "关联工单", "项目规范", "需求分析",
                "测试点", "测试用例", "待确认清单", "关联讨论", "相关讨论") + _L3_LEAD


def _segment_evidence(s: str) -> list[str]:
    """按真实来源切分：引号外遇到句末标点或新的来源引导词就另起一段；引号内一律不断（保住原句完整）。"""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch in _Q_OPEN:
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch in _Q_CLOSE:
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if depth == 0:
            if ch in _BREAK_PUNCT:                 # 句末标点（引号外）→ 断段
                if ch != "\n":
                    buf.append(ch)
                seg = "".join(buf).strip()
                if seg:
                    out.append(seg)
                buf = []
                i += 1
                continue
            if buf and any(s.startswith(w, i) for w in _SOURCE_LEAD):  # 新来源引导词 → 断段
                seg = "".join(buf).strip()
                if seg:
                    out.append(seg)
                buf = []                           # 引导词归入下一段（下方正常追加本字符）
        buf.append(ch)
        i += 1
    seg = "".join(buf).strip()
    if seg:
        out.append(seg)
    return out


def evidence(text: str) -> str:
    """依据/来源 专用清洗（展示层，不改盘上内容）：
    - 先过 humanize（文件名映射成业务词、去 §/hex/JQL/行号等开发引用）；
    - 整段删除“已检索/未命中…”等搜索过程叙述（非真实来源）；
    - 删除只剩来源标签的悬空碎片（如 业务背景 原句）；
    - 按真实来源分行、引号内不断行，便于阅读核对。
    只保留真实、可核对的来源（引号原句 / 关联工单方案 / 评论原话 / 知识库规则）。"""
    if not text:
        return text
    s = humanize(str(text))
    kept: list[str] = []
    for seg in _segment_evidence(s):
        seg = re.sub(r"`[^`]*`", "", seg)                  # 去残留反引号块
        seg = re.sub(r"[ \t]{2,}", " ", seg).strip(_TRIM_CHARS)
        if not seg:
            continue
        if _PROCESS_HINT.search(seg):                      # 搜索过程/JQL 叙述 → 整段删
            continue
        if _L3_LEAD_RE.match(seg):                         # L3 客户/背景类来源不是有效依据 → 整段删
            continue
        if len(_LABEL_WORDS.sub("", seg)) < 3:             # 只剩悬空来源标签 → 删
            continue
        kept.append(seg)
    return "\n".join(kept)


def clean_review(md: str) -> str:
    """『复核』markdown 清洗（展示层）：humanize（去 §/JQL/hex/文件名）+ 逐行删掉纯搜索
    过程叙述行（含 JQL 或“已检索/未命中…”且无引号原句的行），保留标题/结论/正文结构。"""
    if not md:
        return md
    out: list[str] = []
    for ln in humanize(str(md)).splitlines():
        body = re.sub(r"`[^`]*`", "", ln)
        has_quote = re.search(r"[「『][^」』]+[」』]|[\"“][^\"”]+[\"”]", body)
        if _PROCESS_HINT.search(body) and not has_quote:
            continue
        out.append(ln)
    return "\n".join(out)
