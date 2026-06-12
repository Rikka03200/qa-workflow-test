"""questions.md 闸门服务：解析 3 形态 + 外科手术式改答案 + normalize/validate 硬闸 + 乐观并发原子写。

严守 validate-questions.py 的格式契约。保存只动「✅ 答案」区，绝不重写只读字段，
避免破坏多行内容（来源/可能场景常含多行/列表）。保存前临时文件先 normalize 再
validate（与 qa_pipeline.ensure_questions_format 同款），FAIL 则不落盘。
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Optional

from . import artifacts, scripts_loader

QUESTION_RE = re.compile(r"^## Q(\d+):\s*(.*?)\s*$")
FIELD_RE = re.compile(r"^\*\*(问题|来源|可能场景|影响范围|✅ 答案)\*\*：\s*(.*)$")
AUTO_HINT_RE = re.compile(r"^>\s*已自动检索无据")
AUTO_RESOLVED_RE = re.compile(r"（据.+?自动消解）")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
PLACEHOLDER = "<!-- 待填 -->"


def _has_meaningful(text: str) -> bool:
    stripped = COMMENT_RE.sub("", text)
    for line in stripped.splitlines():
        s = line.strip()
        if s and not AUTO_HINT_RE.match(s):
            return True
    return False


_OPT_RE = re.compile(r"^[\-*•\s]*([A-Z])[.、)）]\s*(.+)$")
_SUPP_RE = re.compile(r"\n?\s*补充[:：]\s*", re.S)


def _parse_options(scenario: str) -> list[dict]:
    """从「可能场景」解析选项（A./B、/C) …）。≥2 个才视为选择题。"""
    out = []
    for line in (scenario or "").splitlines():
        m = _OPT_RE.match(line.strip())
        if m:
            out.append({"key": m.group(1), "text": m.group(2).strip()})
    return out if len(out) >= 2 else []


def _split_supplement(ans: str) -> tuple[str, str]:
    parts = _SUPP_RE.split(ans or "", maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return (ans or "").strip(), ""


def _match_option(base: str, options: list[dict]) -> str:
    """把既有答案对到某选项 key；对不上但非空 → '__custom__'；空 → ''。"""
    b = (base or "").strip()
    if not b:
        return ""
    for o in options:
        k, t = o["key"], o["text"].strip()
        if b == k or any(b.startswith(k + sep) for sep in (".", "、", ")", "）", " ", "．")):
            return k
        if t and (b == t or t in b or b in t):
            return k
    return "__custom__"


def parse(path: Path) -> dict:
    """把 questions.md 解析成可渲染结构 + 保存所需的行定位。"""
    if not path.exists():
        return {"form": "missing", "blocks": [], "counts": {}, "title": "", "intro_present": False}
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    ticket = path.parent.name
    title = lines[0].strip() if lines else ""
    intro_present = any(l.strip().startswith("> 请在每个") for l in lines)

    # 题块起止
    q_idx = [i for i, l in enumerate(lines) if QUESTION_RE.match(l)]
    if not q_idx:
        body = "\n".join(lines[1:]).strip()
        if body in ("无", "无。"):
            form = "none"
        elif "|" in body and "---" in body:
            form = "legacy"  # legacy markdown 表格
        else:
            form = "legacy" if body else "none"
        return {"form": form, "blocks": [], "counts": {}, "title": title,
                "ticket": ticket, "intro_present": intro_present, "raw": text}

    blocks = []
    for bi, start in enumerate(q_idx):
        end = q_idx[bi + 1] if bi + 1 < len(q_idx) else len(lines)
        m = QUESTION_RE.match(lines[start])
        num, qtitle = int(m.group(1)), m.group(2).strip()
        block = lines[start:end]

        # 解析字段：记录每个字段标签所在的块内相对行
        field_pos: list[tuple[str, int, str]] = []
        for i, line in enumerate(block):
            fm = FIELD_RE.match(line)
            if fm:
                field_pos.append((fm.group(1), i, fm.group(2)))
        fields = {"问题": "", "来源": "", "可能场景": "", "影响范围": "", "✅ 答案": ""}
        for idx, (name, pos, inline) in enumerate(field_pos):
            fend = field_pos[idx + 1][1] if idx + 1 < len(field_pos) else len(block)
            raw_content = ([inline] if inline else []) + block[pos + 1:fend]
            # 过滤掉「已自动检索无据」提示行——它插在 影响范围 与 答案 之间，不属于任一只读字段内容
            content = [ln for ln in raw_content if not AUTO_HINT_RE.match(ln.strip())]
            fields[name] = "\n".join(content).strip()

        answer = fields["✅ 答案"]
        needs_human = any(AUTO_HINT_RE.match(l.strip()) for l in block)
        if AUTO_RESOLVED_RE.search(answer):
            state = "auto"
        elif _has_meaningful(answer):
            state = "human"
        else:
            state = "placeholder"

        # 答案标签的绝对行号（用于外科手术替换）
        ans_label_abs = next((start + p for (n, p, _) in field_pos if n == "✅ 答案"), None)
        answer_display = COMMENT_RE.sub("", answer).strip()
        options = _parse_options(fields["可能场景"])
        answer_base, supplement = _split_supplement(answer_display)
        if options:
            selected = _match_option(answer_base, options)
        else:
            selected = "__custom__" if answer_base else ""
        custom_text = answer_base if selected == "__custom__" else ""
        blocks.append({
            "num": num, "title": qtitle,
            "problem": fields["问题"], "source": fields["来源"],
            "scenario": fields["可能场景"], "impact": fields["影响范围"],
            "answer": answer, "answer_display": answer_display,
            "options": options, "selected": selected,
            "custom_text": custom_text, "supplement": supplement,
            "state": state, "needs_human": needs_human,
            "label_line": ans_label_abs, "block_end": end,
        })

    counts = {
        "total": len(blocks),
        "human": sum(1 for b in blocks if b["state"] == "human"),
        "auto": sum(1 for b in blocks if b["state"] == "auto"),
        "pending": sum(1 for b in blocks if b["state"] == "placeholder"),
    }
    return {"form": "questions", "blocks": blocks, "counts": counts, "title": title,
            "ticket": ticket, "intro_present": intro_present, "raw": text}


def _normalize_and_validate(tmp: Path) -> list:
    """临时文件先 normalize 再 validate；返回 issues（FAIL 即不可落盘）。"""
    nq = scripts_loader.normalize_questions()
    vq = scripts_loader.validate_questions()
    try:
        nq.normalize_file(tmp, write=True)
    except Exception:  # noqa: BLE001
        pass
    return vq.validate(tmp)


def validate_text(path: Path, text: str) -> list:
    """不落盘预校验：写同目录临时文件（validate 依赖 parent.name=工单号）→ 校验 → 删。"""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.precheck.tmp")
    # validate 用 path.parent.name 当工单号，故临时文件必须与 questions.md 同目录
    tmp.write_text(text, encoding="utf-8")
    try:
        vq = scripts_loader.validate_questions()
        return vq.validate(tmp)
    finally:
        tmp.unlink(missing_ok=True)


def needs_resume(ticket_dir: Path) -> bool:
    """questions.md is newer than test-design.json, or design does not exist yet."""
    qpath = ticket_dir / "questions.md"
    tdpath = ticket_dir / "test-design.json"
    if not qpath.exists():
        return False
    if not tdpath.exists():
        return True
    try:
        if qpath.stat().st_mtime_ns <= tdpath.stat().st_mtime_ns:
            return False
    except OSError:
        return False
    parsed = parse(qpath)
    return parsed.get("form") != "none"


def save_answers(path: Path, answers: dict[int, str], client_mtime_ns: int) -> dict:
    """外科手术式写回各题答案区 + normalize + validate + 乐观并发 + 原子写。

    answers: {题号: 答案文本}。空文本且原为人工/自动消解答案 → 不动（防误清空）。
    """
    if not path.exists():
        return {"ok": False, "kind": "not_found", "error": "questions.md 不存在"}

    cur_ns = path.stat().st_mtime_ns
    if client_mtime_ns and cur_ns != client_mtime_ns:
        return {"ok": False, "kind": "conflict",
                "error": "文件已被他人或批量任务更新，请刷新后重试。"}

    parsed = parse(path)
    if parsed["form"] != "questions":
        return {"ok": False, "kind": "form", "error": "当前 questions.md 不是标准题块形态，请先「规范化」。"}

    lines = parsed["raw"].splitlines()
    # 从后往前替换，避免行号位移
    for block in sorted(parsed["blocks"], key=lambda b: b["num"], reverse=True):
        num = block["num"]
        if num not in answers:
            continue
        submitted = (answers.get(num) or "").strip()
        label = block["label_line"]
        if label is None:
            continue
        if not submitted and block["state"] in ("human", "auto"):
            continue  # 防误清空既有答案
        content = submitted if submitted else PLACEHOLDER
        # 替换 [label+1, block_end) 为新答案内容 + 一个空行分隔
        new_seg = [c for c in content.splitlines()] + [""]
        lines[label] = "**✅ 答案**："  # 规范化标签行（去掉可能的行内答案）
        lines[label + 1:block["block_end"]] = new_seg

    new_text = "\n".join(lines).rstrip() + "\n"
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    issues = _normalize_and_validate(tmp)
    fails = [i for i in issues if i.level == "FAIL"]
    if fails:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "kind": "validation",
                "error": "保存被校验闸门拦下（未落盘）。",
                "issues": [{"level": i.level, "message": i.message} for i in issues]}

    # 落盘前再核一次 mtime（处理处理期间的竞态）
    if client_mtime_ns and path.stat().st_mtime_ns != client_mtime_ns:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "kind": "conflict", "error": "文件刚被更新，请刷新后重试。"}
    os.replace(tmp, path)
    artifacts.mirror_file(path)
    return {"ok": True, "issues": [{"level": i.level, "message": i.message} for i in issues if i.level == "WARN"]}


def normalize_preview(path: Path, text: str) -> tuple[str, bool]:
    """规范化预览（不落盘），供 legacy 表格/raw 编辑。"""
    nq = scripts_loader.normalize_questions()
    return nq.normalize_text(path, text)


def save_raw(path: Path, text: str, client_mtime_ns: int) -> dict:
    """raw 文本保存（legacy/直接编辑）：normalize + validate + 乐观并发 + 原子写。"""
    if path.exists() and client_mtime_ns and path.stat().st_mtime_ns != client_mtime_ns:
        return {"ok": False, "kind": "conflict", "error": "文件已被更新，请刷新后重试。"}
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    issues = _normalize_and_validate(tmp)
    fails = [i for i in issues if i.level == "FAIL"]
    if fails:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "kind": "validation", "error": "校验未通过（未落盘）。",
                "issues": [{"level": i.level, "message": i.message} for i in issues]}
    if path.exists() and client_mtime_ns and path.stat().st_mtime_ns != client_mtime_ns:
        tmp.unlink(missing_ok=True)
        return {"ok": False, "kind": "conflict", "error": "文件刚被更新，请刷新后重试。"}
    os.replace(tmp, path)
    artifacts.mirror_file(path)
    return {"ok": True}
