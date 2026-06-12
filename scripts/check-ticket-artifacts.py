#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "tickets"

TICKET_RE = re.compile(r"^[A-Z]+-\d+$")
UUID_RE = re.compile(r"^[0-9A-F]{32}$")
FORBIDDEN_HTML_RE = re.compile(r"</?(?:b|strong)\b|《\s*/?\s*span\b|《\s*/?\s*spanspan\b|&(?:lt|gt|quot);", re.I)
COMPLETION_RE = re.compile(r"(?:^|\n)\s*(?:✓|✅).*|请审增删|确认后运行|下一步[:：].*/qa:|人工动作|输出自查报告")
PLACEHOLDER_RE = re.compile(r"待执行|TODO|待补|稍后补充|（待回填）|\(待回填\)", re.I)
BAD_JIRA_HEADING_RE = re.compile(r"^#\s+(?:客户|背景|问题|方案|修改|优化|缺少|在|仓配|分拣|入库|出库|WMS|APP)", re.M)

REQUIRED_FILES = [
    "README.md",
    "requirement.md",
    "linked-issues.md",
    "business-context.md",
    "analysis.md",
    "test-points.md",
    "test-design.json",
]

EXPECTED_HEADINGS = {
    "README.md": ["## SOP 进度", "## 关键决策记录", "## 附加材料"],
    "requirement.md": [
        "## 1. 基本信息",
        "## 2. 客户与背景",
        "## 3. 修改方案",
        "## 4. 关联工单",
        "## 5. 评论关键摘录",
        "## 6. 原型图链接",
        "## 7. 附件元数据",
        "## 8. Q&A",
    ],
    "linked-issues.md": ["## 1. 直接关联工单", "## 2. 参考工单", "## 3. 已搜索 JQL"],
    "business-context.md": [
        "## 0. 关键词识别",
        "## 1. 涉及的术语",
        "## 2. 涉及的模块路径",
        "## 3. 涉及的业务规则",
        "## 4. Confluence 检索结果",
        "## 5. Jira 搜索记录",
        "## 6. 与历史规则的关系",
        "## 7. 用例编写约束",
    ],
    "analysis.md": [
        "## 1. 功能清单",
        "## 2. 方案切面清单",
        "## 3. 风险与影响分析",
        "## 4. 待确认问题清单",
        "## 5. 测试范围",
        "## 6. 测试点数量预估",
    ],
    "questions.md": ["**问题**", "**来源**", "**可能场景**", "**影响范围**", "**✅ 答案**"],
    "test-points.md": [
        "## 1. Pre-flight 5 问",
        "## 2. 测试点列表",
        "### 2.1 功能验证",
        "### 2.2 边界与字段校验",
        "### 2.3 异常路径",
        "### 2.4 权限矩阵",
        "### 2.5 与历史功能联动",
        "### 2.6 数据迁移",
        "## 3. 容器路径",
        "## 4. 不覆盖范围",
        "## 5. 仍未决问题",
    ],
}


@dataclass
class Issue:
    level: str
    path: Path
    message: str


class Reporter:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    def fail(self, path: Path, message: str) -> None:
        self.issues.append(Issue("FAIL", path, message))

    def warn(self, path: Path, message: str) -> None:
        self.issues.append(Issue("WARN", path, message))


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def read_text(path: Path, reporter: Reporter) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        reporter.fail(path, "文件不是有效 UTF-8")
    except OSError as exc:
        reporter.fail(path, f"无法读取文件：{exc}")
    return None


def ticket_dirs(root: Path) -> list[Path]:
    if (root / "requirement.md").exists() or (root / "test-design.json").exists():
        return [root]

    direct_ticket_dirs = sorted(path for path in root.iterdir() if path.is_dir() and TICKET_RE.fullmatch(path.name))
    if direct_ticket_dirs:
        return direct_ticket_dirs

    return sorted(path for path in root.glob("*/*/*") if path.is_dir() and TICKET_RE.fullmatch(path.name))


def check_directory(path: Path, reporter: Reporter) -> None:
    for name in REQUIRED_FILES:
        if not (path / name).exists():
            reporter.fail(path / name, "缺少标准产物文件")
    if not (path / "attachments").is_dir():
        reporter.warn(path / "attachments", "缺少 attachments/ 空目录占位")

    for extra in sorted(p for p in path.iterdir() if p.is_file() and p.name not in REQUIRED_FILES and p.name != "questions.md"):
        reporter.warn(extra, "额外材料文件，建议在 README.md 的“附加材料”登记")


def check_questions_format(path: Path, reporter: Reporter) -> None:
    """Run the dedicated questions.md validator.

    Existing historical tickets contain older questions.md shapes. During the
    transition, artifact checks report dedicated-validator failures as WARN;
    qa_pipeline gates new generation strictly before points/design.
    """
    validator = REPO_ROOT / "scripts" / "validate-questions.py"
    if not validator.exists():
        reporter.warn(path, "缺少 validate-questions.py，无法校验 questions.md 稳定格式")
        return
    r = subprocess.run(
        [sys.executable, str(validator), str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if r.returncode != 0:
        lines = [line for line in ((r.stdout or "") + (r.stderr or "")).splitlines() if line.startswith(("FAIL:", "WARN:"))]
        if lines:
            for line in lines[:10]:
                reporter.warn(path, "questions.md 稳定格式待迁移：" + line.split(": ", 2)[-1])
        else:
            reporter.warn(path, "questions.md 稳定格式校验未通过")


def check_markdown(path: Path, reporter: Reporter) -> None:
    text = read_text(path, reporter)
    if text is None:
        return
    if not text.strip():
        reporter.fail(path, "Markdown 文件为空")
        return

    headings1 = re.findall(r"^#\s+", text, re.M)
    if not text.startswith("# "):
        reporter.fail(path, "第 1 行必须是一级标题")
    if len(headings1) != 1:
        reporter.fail(path, f"只能有 1 个一级标题，实际 {len(headings1)} 个")

    if FORBIDDEN_HTML_RE.search(text):
        reporter.fail(path, "含禁用 HTML / 损坏 span / HTML 实体")
    if COMPLETION_RE.search(text):
        reporter.warn(path, "疑似混入聊天完成报告或下一步提示")
    if PLACEHOLDER_RE.search(text):
        reporter.warn(path, "含未完成占位；应改为“无”或按产物稳定格式写入具体待确认问题")

    if path.name == "requirement.md" and BAD_JIRA_HEADING_RE.search(text):
        reporter.fail(path, "疑似保留 Jira wiki 一级标题，破坏 requirement.md 结构")
    if path.name == "questions.md":
        check_questions_format(path, reporter)

    expected = EXPECTED_HEADINGS.get(path.name, [])
    if path.name == "questions.md" and text.strip().endswith("无"):
        return
    for heading in expected:
        if heading not in text:
            reporter.warn(path, f"缺少标准章节或字段：{heading}")


def check_json(path: Path, reporter: Reporter) -> None:
    text = read_text(path, reporter)
    if text is None:
        return
    if not text.strip():
        reporter.fail(path, "JSON 文件为空")
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        reporter.fail(path, f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}")
        return
    if not isinstance(data, list) or len(data) != 1:
        reporter.fail(path, "test-design.json 必须是长度为 1 的数组")
        return

    ids: set[str] = set()
    ticket = path.parent.name
    root = data[0]
    if not isinstance(root, dict):
        reporter.fail(path, "根节点必须是对象")
        return
    if root.get("text") != ticket:
        reporter.fail(path, "根节点 text 必须等于工单号")
    if root.get("side") != "right":
        reporter.fail(path, "根节点 side 必须为 right")

    def walk(node: Any, node_path: str, parent_is_test_point: bool = False, index: int = 0) -> None:
        if not isinstance(node, dict):
            reporter.fail(path, f"{node_path}: 节点必须是对象")
            return
        node_id = node.get("id")
        if not isinstance(node_id, str) or not UUID_RE.fullmatch(node_id):
            reporter.fail(path, f"{node_path}: id 必须是 32 位大写十六进制")
        elif node_id in ids:
            reporter.fail(path, f"{node_path}: id 重复 {node_id}")
        else:
            ids.add(node_id)

        text_value = node.get("text")
        if isinstance(text_value, str) and FORBIDDEN_HTML_RE.search(text_value):
            reporter.fail(path, f"{node_path}: text 含禁用 HTML")

        is_condition = node.get("condition") == "Y"
        is_step = node.get("step") == "Y"
        is_expect = node.get("expect") == "Y"
        is_test_point = "mark" in node or "testPoint" in node

        if is_condition:
            if index != 0:
                reporter.fail(path, f"{node_path}: condition 必须位于第一位")
            if "children" in node:
                reporter.fail(path, f"{node_path}: condition 不能包含 children")
        if is_expect and "children" in node:
            reporter.fail(path, f"{node_path}: expect 不能包含 children")
        if is_step:
            children = node.get("children")
            if not isinstance(children, list) or len(children) != 1 or not isinstance(children[0], dict) or children[0].get("expect") != "Y":
                reporter.fail(path, f"{node_path}: step 必须恰好包含 1 个 expect")
        if is_test_point:
            tp = node.get("testPoint")
            tp_id = tp.get("id") if isinstance(tp, dict) else None
            if not isinstance(tp_id, str) or not UUID_RE.fullmatch(tp_id):
                reporter.fail(path, f"{node_path}: testPoint.id 必须是 32 位大写十六进制")
            priority = node.get("mark", {}).get("priority") if isinstance(node.get("mark"), dict) else None
            if not isinstance(priority, dict) or len([k for k, v in priority.items() if k in {"1", "2", "3"} and v is True]) != 1:
                reporter.fail(path, f"{node_path}: mark.priority 必须且只能启用一个优先级")

        if is_condition or is_expect:
            return
        for child_index, child in enumerate(node.get("children", []) if isinstance(node.get("children"), list) else []):
            walk(child, f"{node_path}.children[{child_index}]", is_test_point, child_index)

    walk(root, "$[0]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check QA ticket artifacts for Markdown and JSON contract compliance.")
    parser.add_argument("paths", nargs="*", help="工单目录或 tickets 根目录；不传则扫描 tickets/")
    parser.add_argument("--strict", action="store_true", help="将 WARN 也作为失败退出")
    parser.add_argument("--summary-only", action="store_true", help="只输出按工单聚合的摘要，适合检查整个 Sprint")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    roots = [Path(p).resolve() for p in args.paths] if args.paths else [DEFAULT_ROOT]
    reporter = Reporter()

    dirs: list[Path] = []
    for root in roots:
        if not root.exists():
            reporter.fail(root, "路径不存在")
            continue
        dirs.extend(ticket_dirs(root))

    if not dirs:
        reporter.fail(DEFAULT_ROOT, "未找到工单目录")

    for directory in dirs:
        check_directory(directory, reporter)
        for md in ["README.md", "requirement.md", "linked-issues.md", "business-context.md", "analysis.md", "questions.md", "test-points.md"]:
            file_path = directory / md
            if file_path.exists():
                check_markdown(file_path, reporter)
        json_path = directory / "test-design.json"
        if json_path.exists():
            check_json(json_path, reporter)

    failures = [issue for issue in reporter.issues if issue.level == "FAIL"]
    warnings = [issue for issue in reporter.issues if issue.level == "WARN"]

    if args.summary_only:
        by_ticket: dict[str, dict[str, int]] = {}
        for issue in reporter.issues:
            try:
                ticket = next(part for part in issue.path.parts if TICKET_RE.fullmatch(part))
            except StopIteration:
                ticket = rel(issue.path)
            counts = by_ticket.setdefault(ticket, {"FAIL": 0, "WARN": 0})
            counts[issue.level] += 1
        for ticket, counts in sorted(by_ticket.items()):
            print(f"{ticket}: FAIL={counts['FAIL']} WARN={counts['WARN']}")
    else:
        for issue in reporter.issues:
            print(f"{issue.level}: {rel(issue.path)}: {issue.message}")

    print("\n=== 汇总 ===")
    print(f"工单目录：{len(dirs)}")
    print(f"失败：{len(failures)}")
    print(f"警告：{len(warnings)}")

    if failures or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
