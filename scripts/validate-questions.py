#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate questions.md deterministic format.

This validator is intentionally structural only: it checks the human-answer
entry format is stable, but does not judge business correctness.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

QUESTION_RE = re.compile(r"^## Q(\d+):\s*(.+?)\s*$")
H2_RE = re.compile(r"^##\s+")
FIELD_ORDER = ["问题", "来源", "可能场景", "影响范围", "✅ 答案"]
FIELD_RE = re.compile(r"^\*\*(问题|来源|可能场景|影响范围|✅ 答案)\*\*：\s*(.*)$")
INTRO_LINE = "> 请在每个“✅ 答案”下方填写确认结果；如果暂时无法确认，填写 `[待确认]`。"
FORBIDDEN_HTML_RE = re.compile(r"</?(?:b|strong)\b|《\s*/?\s*span\b|《\s*/?\s*spanspan\b|&(?:lt|gt|quot);", re.I)
TODO_RE = re.compile(r"TODO|待补|稍后补充|（待回填）|\(待回填\)", re.I)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
AUTO_HINT_RE = re.compile(r"^>\s*已自动检索无据")


@dataclass
class Issue:
    level: str
    path: Path
    message: str


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"FAIL: {path}: 文件不是有效 UTF-8")
    except OSError as exc:
        print(f"FAIL: {path}: 无法读取文件：{exc}")
    return None


def _strip_comments(text: str) -> str:
    return COMMENT_RE.sub("", text)


def _has_meaningful_text(text: str, allow_auto_hint: bool = True) -> bool:
    lines = []
    for line in _strip_comments(text).splitlines():
        s = line.strip()
        if not s:
            continue
        if allow_auto_hint and AUTO_HINT_RE.match(s):
            continue
        lines.append(s)
    return bool(lines)


def _issue(level: str, path: Path, message: str, issues: list[Issue]) -> None:
    issues.append(Issue(level, path, message))


def _validate_no_questions(path: Path, body: str, issues: list[Issue]) -> None:
    if body.strip() != "无":
        _issue("FAIL", path, "无待确认问题时，标题后正文必须且只能写一行“无”", issues)


def _split_question_blocks(path: Path, lines: list[str], issues: list[Issue]) -> list[tuple[int, str, list[str]]]:
    blocks: list[tuple[int, str, list[str]]] = []
    current_num: int | None = None
    current_title = ""
    current_lines: list[str] = []

    for line in lines:
        m = QUESTION_RE.match(line)
        if m:
            if current_num is not None:
                blocks.append((current_num, current_title, current_lines))
            current_num = int(m.group(1))
            current_title = m.group(2).strip()
            current_lines = []
            if not current_title:
                _issue("FAIL", path, f"Q{current_num}: 标题不能为空", issues)
            continue
        if H2_RE.match(line):
            _issue("FAIL", path, f"不允许非题块二级标题：{line.strip()}；补充问题也必须写成连续 ## QN: ... 题块", issues)
        if current_num is None:
            if line.strip() and not line.strip().startswith(">"):
                if line.strip() in {"无", "无。"}:
                    _issue("FAIL", path, "有题块时不得在题块前同时写“无”", issues)
                else:
                    _issue("WARN", path, f"题块前存在非标准说明行：{line.strip()[:60]}", issues)
        else:
            current_lines.append(line)

    if current_num is not None:
        blocks.append((current_num, current_title, current_lines))
    return blocks


def _parse_fields(path: Path, qnum: int, block_lines: list[str], issues: list[Issue]) -> dict[str, str]:
    positions: list[tuple[str, int, str]] = []
    for i, line in enumerate(block_lines):
        m = FIELD_RE.match(line)
        if m:
            positions.append((m.group(1), i, m.group(2)))
        elif line.startswith("**") and "**" in line[2:] and "：" in line:
            _issue("FAIL", path, f"Q{qnum}: 非标准字段标签或字段漂移：{line.strip()}", issues)

    names = [name for name, _, _ in positions]
    if names != FIELD_ORDER:
        _issue("FAIL", path, f"Q{qnum}: 字段必须且只能按顺序出现：{', '.join('**'+x+'**：' for x in FIELD_ORDER)}；实际：{names or '无'}", issues)

    fields: dict[str, str] = {name: "" for name in FIELD_ORDER}
    for idx, (name, pos, same_line) in enumerate(positions):
        end = positions[idx + 1][1] if idx + 1 < len(positions) else len(block_lines)
        content = [same_line] if same_line else []
        content.extend(block_lines[pos + 1:end])
        fields[name] = "\n".join(content).strip()

    return fields


def validate(path: Path) -> list[Issue]:
    issues: list[Issue] = []
    text = _read(path)
    if text is None:
        issues.append(Issue("FAIL", path, "无法读取"))
        return issues
    if not text.strip():
        _issue("FAIL", path, "questions.md 不能为空", issues)
        return issues
    if FORBIDDEN_HTML_RE.search(text):
        _issue("FAIL", path, "含禁用 HTML / 损坏 span / HTML 实体", issues)
    if TODO_RE.search(text):
        _issue("FAIL", path, "含 TODO/待补/待回填 等未完成占位", issues)

    lines = text.splitlines()
    ticket = path.parent.name
    expected_title = f"# 待确认问题清单 — {ticket}"
    if not lines or lines[0].strip() != expected_title:
        _issue("FAIL", path, f"第 1 行必须固定为：{expected_title}", issues)
    h1_count = sum(1 for line in lines if line.startswith("# "))
    if h1_count != 1:
        _issue("FAIL", path, f"只能有 1 个一级标题，实际 {h1_count} 个", issues)

    body = "\n".join(lines[1:]).strip()
    has_q = any(QUESTION_RE.match(line) for line in lines)
    if not has_q:
        _validate_no_questions(path, body, issues)
        return issues

    intro_lines = [line.strip() for line in lines[1:] if line.strip().startswith(">")]
    if INTRO_LINE not in intro_lines:
        _issue("FAIL", path, f"有待确认问题时，标题下必须包含固定说明行：{INTRO_LINE}", issues)
    for line in intro_lines:
        if line != INTRO_LINE and not AUTO_HINT_RE.match(line):
            _issue("WARN", path, f"存在非标准引用说明行：{line[:80]}", issues)

    blocks = _split_question_blocks(path, lines[1:], issues)
    nums = [num for num, _, _ in blocks]
    expected_nums = list(range(1, len(blocks) + 1))
    if nums != expected_nums:
        _issue("FAIL", path, f"题号必须从 Q1 连续递增；实际 {nums}，期望 {expected_nums}", issues)

    for qnum, title, block_lines in blocks:
        fields = _parse_fields(path, qnum, block_lines, issues)
        for name in ["问题", "来源", "可能场景", "影响范围"]:
            if not _has_meaningful_text(fields.get(name, ""), allow_auto_hint=False):
                _issue("FAIL", path, f"Q{qnum}: **{name}** 字段正文不能为空", issues)
        answer = fields.get("✅ 答案", "")
        if "**" in answer and any(f"**{n}**：" in answer for n in FIELD_ORDER[:-1]):
            _issue("FAIL", path, f"Q{qnum}: 答案区疑似混入其他字段标签", issues)
        if not answer.strip():
            _issue("FAIL", path, f"Q{qnum}: **✅ 答案** 后必须有人工答案、自动消解答案或 HTML 注释占位", issues)
        elif not _has_meaningful_text(answer) and "<!--" not in answer:
            _issue("FAIL", path, f"Q{qnum}: 答案区为空且无 HTML 注释占位", issues)

    return issues


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate deterministic questions.md format.")
    ap.add_argument("paths", nargs="+", help="questions.md file(s)")
    ap.add_argument("--strict", action="store_true", help="WARN also causes non-zero exit")
    args = ap.parse_args()

    all_issues: list[Issue] = []
    for p in args.paths:
        all_issues.extend(validate(Path(p).resolve()))

    for issue in all_issues:
        print(f"{issue.level}: {issue.path}: {issue.message}")

    failures = [i for i in all_issues if i.level == "FAIL"]
    warnings = [i for i in all_issues if i.level == "WARN"]
    print("\n=== 汇总 ===")
    print(f"失败：{len(failures)}")
    print(f"警告：{len(warnings)}")
    return 1 if failures or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
