#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Normalize questions.md to the deterministic human-answer format.

Safe structural fixes only. This script does NOT invent missing business content
and does NOT change human answers. If content is too ambiguous, validation after
normalization will still fail and block the pipeline.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

INTRO_LINE = "> 请在每个“✅ 答案”下方填写确认结果；如果暂时无法确认，填写 `[待确认]`。"
QUESTION_RE = re.compile(r"^##\s*Q(\d+)(?::|：)?\s*(.*?)\s*$")
ANY_H2_RE = re.compile(r"^##\s+")
FIELD_PATTERNS = [
    ("问题", re.compile(r"^\*\*\s*问题\s*\*\*\s*[:：]\s*(.*)$")),
    ("来源", re.compile(r"^\*\*\s*来源\s*\*\*\s*[:：]\s*(.*)$")),
    ("可能场景", re.compile(r"^\*\*\s*可能场景\s*\*\*\s*[:：]\s*(.*)$")),
    ("影响范围", re.compile(r"^\*\*\s*影响范围\s*\*\*\s*[:：]\s*(.*)$")),
    ("✅ 答案", re.compile(r"^\*\*\s*(?:✅\s*)?答案\s*\*\*\s*[:：]\s*(.*)$")),
]
FIELD_ORDER = ["问题", "来源", "可能场景", "影响范围", "✅ 答案"]


def _normalize_field_line(line: str) -> tuple[str, str] | None:
    s = line.rstrip()
    for name, pattern in FIELD_PATTERNS:
        m = pattern.match(s)
        if m:
            return name, m.group(1).strip()
    return None


def _normalize_title(path: Path, lines: list[str]) -> tuple[list[str], bool]:
    expected = f"# 待确认问题清单 — {path.parent.name}"
    changed = False
    if not lines:
        return [expected], True
    if lines[0].strip() != expected:
        lines = [expected] + lines[1:]
        changed = True
    return lines, changed


def _has_questions(lines: list[str]) -> bool:
    return any(QUESTION_RE.match(line.strip()) for line in lines)


def _normalize_no_questions(path: Path, lines: list[str]) -> tuple[str, bool]:
    title = f"# 待确认问题清单 — {path.parent.name}"
    body = "\n".join(lines[1:]).strip()
    if body in {"无", "无。"}:
        normalized = f"{title}\n\n无\n"
        return normalized, normalized != "\n".join(lines).rstrip() + "\n"
    # Too ambiguous to rewrite safely; only normalize title and leave body.
    normalized = "\n".join([title, "", body]).rstrip() + "\n"
    return normalized, normalized != "\n".join(lines).rstrip() + "\n"


def _normalize_question_heading(line: str, qnum: int) -> tuple[str, bool]:
    m = QUESTION_RE.match(line.strip())
    if not m:
        return line.rstrip(), False
    title = (m.group(2) or "").strip()
    # Do not invent a placeholder title. Empty titles must remain invalid so
    # validate-questions.py can block instead of silently creating fake content.
    new = f"## Q{qnum}: {title}"
    return new, new != line.rstrip()


def normalize_text(path: Path, text: str) -> tuple[str, bool]:
    raw_lines = text.splitlines()
    lines, changed = _normalize_title(path, raw_lines)
    if not _has_questions(lines):
        return _normalize_no_questions(path, lines)

    out: list[str] = [f"# 待确认问题清单 — {path.parent.name}", "", INTRO_LINE, ""]
    qnum = 0
    in_question = False
    pending_blank = False

    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            pending_blank = True
            continue
        if stripped == INTRO_LINE:
            continue
        if stripped.startswith(">") and stripped != INTRO_LINE:
            # Preserve auto no-evidence hints inside a question; skip other intro-like blockquotes before Q.
            if in_question:
                if pending_blank and out and out[-1] != "":
                    out.append("")
                out.append(stripped)
                pending_blank = False
            continue

        qm = QUESTION_RE.match(stripped)
        if qm:
            qnum += 1
            if out and out[-1] != "":
                out.append("")
            new_heading, hd_changed = _normalize_question_heading(stripped, qnum)
            out.append(new_heading)
            in_question = True
            pending_blank = False
            changed = changed or hd_changed or int(qm.group(1)) != qnum
            continue

        if ANY_H2_RE.match(stripped):
            # Drop non-question H2 section titles; their child Q blocks are preserved.
            changed = True
            pending_blank = False
            continue

        field = _normalize_field_line(stripped)
        if field:
            name, inline = field
            if out and out[-1] != "":
                out.append("")
            out.append(f"**{name}**：")
            if inline:
                out.append(inline)
            pending_blank = False
            if stripped != f"**{name}**：" or inline:
                changed = True
            continue

        if not in_question:
            # Drop non-standard pre-question prose/separators; validator requires stable entry.
            changed = True
            pending_blank = False
            continue

        if pending_blank and out and out[-1] != "":
            out.append("")
        out.append(line.rstrip())
        pending_blank = False

    normalized = "\n".join(out).rstrip() + "\n"
    changed = changed or normalized != text.rstrip() + "\n"
    return normalized, changed


def normalize_file(path: Path, write: bool = True) -> bool:
    text = path.read_text(encoding="utf-8")
    normalized, changed = normalize_text(path, text)
    if changed and write:
        path.write_text(normalized, encoding="utf-8")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="Normalize questions.md deterministic format without changing business meaning.")
    ap.add_argument("paths", nargs="+", help="questions.md file(s)")
    ap.add_argument("--check", action="store_true", help="Do not write; exit 1 if changes would be made")
    args = ap.parse_args()

    changed_any = False
    for item in args.paths:
        path = Path(item).resolve()
        changed = normalize_file(path, write=not args.check)
        changed_any = changed_any or changed
        print(f"{'CHANGED' if changed else 'OK'}: {path}")
    return 1 if args.check and changed_any else 0


if __name__ == "__main__":
    sys.exit(main())
