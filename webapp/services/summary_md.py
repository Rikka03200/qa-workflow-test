"""极简、安全的 Markdown→HTML 渲染（先转义防注入，只支持常用块/行内）。

仅用于把"需求方案/复核发现"等本就给人看的文本排版好看；不追求完备，够用且零依赖。
支持：# 标题、- / 1. 列表、> 引用、--- 分隔线、空行分段、**粗体**、`行内代码`。
"""

from __future__ import annotations

import html
import re

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+?)`")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_UNORDERED = re.compile(r"^\s*[-*•]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _inline(text: str) -> str:
    t = html.escape(text)
    t = _CODE.sub(r"<code>\1</code>", t)
    t = _BOLD.sub(r"<strong>\1</strong>", t)
    return t


def render(md: str) -> str:
    if not md:
        return ""
    out: list[str] = []
    para: list[str] = []
    list_items: list[str] = []
    list_kind = ""  # "ul" | "ol" | ""

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    def flush_list():
        nonlocal list_kind
        if list_items:
            tag = list_kind or "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{li}</li>" for li in list_items) + f"</{tag}>")
            list_items.clear()
            list_kind = ""

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            flush_list()
            continue
        mh = _HEADING.match(line)
        if mh:
            flush_para(); flush_list()
            level = min(len(mh.group(1)) + 2, 6)  # # → h3，避免与页面 h1/h2 冲突
            out.append(f"<h{level}>{_inline(mh.group(2))}</h{level}>")
            continue
        if line.strip() in ("---", "***", "___"):
            flush_para(); flush_list()
            out.append("<hr>")
            continue
        if line.lstrip().startswith(">"):
            flush_para(); flush_list()
            out.append(f"<blockquote>{_inline(line.lstrip()[1:].strip())}</blockquote>")
            continue
        mo = _ORDERED.match(line)
        mu = _UNORDERED.match(line)
        if mo or mu:
            flush_para()
            kind = "ol" if mo else "ul"
            if list_kind and list_kind != kind:
                flush_list()
            list_kind = kind
            list_items.append(_inline((mo or mu).group(1)))
            continue
        # 普通段落行
        flush_list()
        para.append(line.strip())

    flush_para()
    flush_list()
    return "\n".join(out)
