#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GLOB = "tickets/*/*/*/test-design.json"

UUID_RE = re.compile(r"^[0-9A-F]{32}$")
TICKET_RE = re.compile(r"\b[A-Z]+-\d+\b")
ATTACHMENT_RE = re.compile(r"\b[\w.-]+\.(?:png|jpe?g|gif|webp|bmp|svg|pdf|xlsx?|docx?|zip|rar|7z)\b", re.IGNORECASE)
SOURCE_RE = re.compile(r"\[来源\s*:")
SECTION_RE = re.compile(r"§\s*\d+")
FORBIDDEN_HTML_RE = re.compile(r"</?(?:b|strong)\b|《\s*/?\s*span\b|《\s*/?\s*spanspan\b", re.IGNORECASE)
HTML_ENTITY_RE = re.compile(r"&(?:lt|gt|amp|quot|apos|nbsp);")
PLACEHOLDER_TITLE_RE = re.compile(
    r"(?:场景\s*\d+|样例\s*\d+|example[_\s-]*\d+|case[_\s-]*\d+|回归[_\s-]*[A-Z]+-\d+|bug[_\s-]*\d+|第一种|第二种)",
    re.IGNORECASE,
)
REFERENCE_WORDS = ("requirement.md", "business-context.md", "linked-issues.md", "test-points.md", "Jira", "Confluence")
PROCESS_WORDS = ("已搜索 JQL", "JQL:", "AI 工作过程", "需求方案写", "参考工单", "本工单")
# 与 _kb/_global/case-writing-spec.md §1 + _kb/projects/wms/case-samples/style-notes.md §1
# + _kb/projects/wms/modules.md §2 目录树保持同步；新增平台时同步这里（否则合法平台会被误判 FAIL）。
PLATFORM_PREFIXES = ("web", "仓配app", "采配app", "零售app", "POS", "TMS",
                     "TMS小程序", "供应商平台", "供应商app", "前置仓app")
LOWERCASE_PLATFORM_PREFIXES = ("pos", "tms", "WEB")


@dataclass
class Issue:
    level: str
    path: str
    message: str


class Validator:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.issues: list[Issue] = []
        self.ids: dict[str, str] = {}
        self.test_point_ids: dict[str, str] = {}
        self.test_point_count = 0
        self.step_count = 0

    def fail(self, path: str, message: str) -> None:
        self.issues.append(Issue("FAIL", path, message))

    def warn(self, path: str, message: str) -> None:
        self.issues.append(Issue("WARN", path, message))

    def validate(self) -> list[Issue]:
        try:
            text = self.file_path.read_text(encoding="utf-8")
        except OSError as exc:
            self.fail("$", f"无法读取文件：{exc}")
            return self.issues

        if not text.strip():
            self.fail("$", "文件为空")
            return self.issues

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            self.fail("$", f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}")
            return self.issues

        if not isinstance(data, list):
            self.fail("$", "根结构必须是数组")
            return self.issues
        if len(data) != 1:
            self.fail("$", f"根数组长度必须为 1，实际为 {len(data)}")
        if data:
            self.validate_node(data[0], "$[0]", parent=None, index=0)
            self.validate_root(data[0])

        if self.test_point_count == 0 and data:
            self.warn("$", "未发现测试点节点")
        return self.issues

    def validate_root(self, node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("side") != "right":
            self.fail("$[0].side", '根节点 side 必须为 "right"')
        for extra_key in ("condition", "step", "expect", "mark", "testPoint"):
            if extra_key in node:
                self.fail(f"$[0].{extra_key}", "根节点不能包含 condition、step、expect、mark 或 testPoint 标记")
        text = node.get("text")
        if not isinstance(text, str) or not TICKET_RE.fullmatch(text.strip()):
            self.fail("$[0].text", "根节点 text 必须是工单号，如 ABC-246155")
        if not isinstance(node.get("children"), list) or not node.get("children"):
            self.fail("$[0].children", "根节点必须包含非空 children")

    def validate_node(self, node: Any, path: str, parent: dict[str, Any] | None, index: int) -> None:
        if not isinstance(node, dict):
            self.fail(path, "节点必须是对象")
            return

        node_id = node.get("id")
        if not isinstance(node_id, str) or not UUID_RE.fullmatch(node_id):
            self.fail(f"{path}.id", "id 必须是 32 位大写十六进制 UUID")
        elif node_id in self.ids:
            self.fail(f"{path}.id", f"id 与 {self.ids[node_id]} 重复：{node_id}")
        else:
            self.ids[node_id] = f"{path}.id"

        text = node.get("text")
        if not isinstance(text, str) or not text.strip():
            self.fail(f"{path}.text", "text 必须是非空字符串")
        elif FORBIDDEN_HTML_RE.search(text):
            self.fail(f"{path}.text", "text 禁止使用 <b>、<strong> 或损坏 span 标签")
        elif HTML_ENTITY_RE.search(text):
            self.fail(f"{path}.text", "text 禁止使用 HTML 实体转义，需直接写原字符")

        is_condition = node.get("condition") == "Y"
        is_step = node.get("step") == "Y"
        is_expect = node.get("expect") == "Y"
        is_test_point = "mark" in node or "testPoint" in node

        markers = sum([is_condition, is_step, is_expect])
        if markers > 1:
            self.fail(path, "condition、step、expect 标记不能同时出现")

        if is_test_point:
            self.validate_test_point(node, path)
        elif "mark" in node or "testPoint" in node:
            self.fail(path, "测试点节点必须同时包含 mark 和 testPoint")

        if is_condition:
            self.validate_condition(node, path, index)
        elif is_step:
            self.validate_step(node, path)
        elif is_expect:
            self.validate_expect(node, path)
        else:
            self.validate_container(node, path)

        if (is_condition or is_step or is_expect) and isinstance(text, str):
            self.validate_executable_text(text, f"{path}.text")
        if is_test_point and isinstance(text, str):
            self.validate_title(text, f"{path}.text")

        children = node.get("children")
        if isinstance(children, list):
            for child_index, child in enumerate(children):
                if is_expect or is_condition:
                    break
                self.validate_node(child, f"{path}.children[{child_index}]", parent=node, index=child_index)
        elif "children" in node:
            self.fail(f"{path}.children", "children 必须是数组")

    def validate_test_point(self, node: dict[str, Any], path: str) -> None:
        self.test_point_count += 1
        mark = node.get("mark")
        priority = mark.get("priority") if isinstance(mark, dict) else None
        if not isinstance(priority, dict):
            self.fail(f"{path}.mark.priority", "测试点必须包含 mark.priority")
        else:
            enabled = [key for key, value in priority.items() if key in {"1", "2", "3"} and value is True]
            extra = [key for key in priority if key not in {"1", "2", "3"}]
            if len(enabled) != 1 or extra:
                self.fail(f"{path}.mark.priority", 'priority 必须且只能启用 "1"、"2"、"3" 中一个')

        test_point = node.get("testPoint")
        test_point_id = test_point.get("id") if isinstance(test_point, dict) else None
        if not isinstance(test_point_id, str) or not UUID_RE.fullmatch(test_point_id):
            self.fail(f"{path}.testPoint.id", "testPoint.id 必须是 32 位大写十六进制 UUID")
        elif test_point_id in self.test_point_ids:
            self.fail(f"{path}.testPoint.id", f"testPoint.id 与 {self.test_point_ids[test_point_id]} 重复：{test_point_id}")
        else:
            self.test_point_ids[test_point_id] = f"{path}.testPoint.id"

        testcases = node.get("testcases")
        if "testcases" in node and not (isinstance(testcases, list) and all(isinstance(item, str) for item in testcases)):
            self.fail(f"{path}.testcases", "testcases 如出现必须是字符串数组")

        children = node.get("children")
        if not isinstance(children, list) or not children:
            self.fail(f"{path}.children", "测试点必须包含步骤 children")
            return
        condition_indexes = [i for i, child in enumerate(children) if isinstance(child, dict) and child.get("condition") == "Y"]
        if condition_indexes and condition_indexes[0] != 0:
            self.fail(f"{path}.children", "前置条件节点必须位于测试点 children 第一位")
        if len(condition_indexes) > 1:
            self.fail(f"{path}.children", "测试点最多只能有 1 个前置条件节点")
        step_count = 0
        for child_index, child in enumerate(children):
            child_path = f"{path}.children[{child_index}]"
            if not isinstance(child, dict):
                self.fail(child_path, "测试点 children 只能包含 condition 或 step 节点")
                continue
            is_condition = child.get("condition") == "Y"
            is_step = child.get("step") == "Y"
            if is_step:
                step_count += 1
            if not is_condition and not is_step:
                self.fail(child_path, "测试点 children 只能包含 condition 或 step 节点")
        if step_count == 0:
            self.fail(f"{path}.children", "测试点必须至少包含 1 个 step 节点")

    def validate_condition(self, node: dict[str, Any], path: str, index: int) -> None:
        if index != 0:
            self.fail(path, "condition 节点必须位于测试点 children 第一位")
        if "children" in node:
            self.fail(f"{path}.children", "condition 节点不能包含 children")
        text = node.get("text")
        if text and not isinstance(text, str):
            self.fail(f"{path}.text", "condition text 必须是字符串")
        if isinstance(text, str) and "<br>" in text and not re.search(r"(?:^|<br>)\s*\d+\.", text):
            self.warn(f"{path}.text", "多条 condition 建议使用编号条目")

    def validate_step(self, node: dict[str, Any], path: str) -> None:
        self.step_count += 1
        children = node.get("children")
        if not isinstance(children, list):
            self.fail(f"{path}.children", "step 节点必须包含 children 数组")
            return
        expect_indexes = [i for i, child in enumerate(children) if isinstance(child, dict) and child.get("expect") == "Y"]
        if len(children) != 1 or len(expect_indexes) != 1:
            self.fail(f"{path}.children", "每个 step 节点必须恰好包含 1 个 expect 子节点")

    def validate_expect(self, node: dict[str, Any], path: str) -> None:
        if "children" in node:
            self.fail(f"{path}.children", "expect 节点不能包含 children")

    def validate_container(self, node: dict[str, Any], path: str) -> None:
        if path != "$[0]" and "children" not in node:
            self.warn(path, "普通容器节点通常应包含 children")

    def validate_executable_text(self, text: str, path: str) -> None:
        if TICKET_RE.search(text):
            self.fail(path, "condition/step/expect text 禁止出现工单号")
        if ATTACHMENT_RE.search(text):
            self.fail(path, "condition/step/expect text 禁止出现附件文件名")
        if SOURCE_RE.search(text):
            self.fail(path, "condition/step/expect text 禁止出现来源引用")
        if SECTION_RE.search(text):
            self.fail(path, "condition/step/expect text 禁止出现章节引用")
        for word in REFERENCE_WORDS:
            if word in text:
                self.fail(path, f"condition/step/expect text 禁止出现外部文档引用：{word}")
        for word in PROCESS_WORDS:
            if word in text:
                self.fail(path, f"condition/step/expect text 禁止出现 AI 工作过程或元信息：{word}")
        if "`" in text:
            self.fail(path, "condition/step/expect text 禁止使用 markdown 反引号；字段名/按钮/参数值请用中文双引号 “”")

    def validate_title(self, text: str, path: str) -> None:
        if TICKET_RE.search(text):
            self.fail(path, "测试点标题禁止包含工单号")
        if PLACEHOLDER_TITLE_RE.search(text):
            self.fail(path, "测试点标题禁止使用场景编号、样例编号、回归工单号等无信息占位")

        parts = text.split("_")
        if len(parts) < 3 or any(not part.strip() for part in parts):
            self.fail(path, "测试点标题必须符合 {平台}_{模块路径}_{测试要点}，且至少 3 段")
            return

        platform = parts[0]
        if platform not in PLATFORM_PREFIXES:
            if platform in LOWERCASE_PLATFORM_PREFIXES:
                self.fail(path, f"测试点标题平台前缀大小写错误：{platform}")
            else:
                self.fail(path, f"测试点标题平台前缀不在已知 WMS 平台中：{platform}")

        if len(parts[-1].strip()) < 2:
            self.fail(path, "测试点标题末段必须是业务可读的功能点")
        if any(token in text for token in ("`", "<b>", "</b>", "<strong>", "</strong>")):
            self.fail(path, "测试点标题禁止使用 markdown 反引号或 HTML 加粗")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate CodeArts test-design.json files.")
    parser.add_argument("paths", nargs="*", help="test-design.json 文件路径；不传则扫描 tickets/*/*/*/test-design.json")
    parser.add_argument("--quiet", action="store_true", help="只输出失败摘要")
    return parser.parse_args()


def resolve_paths(raw_paths: list[str]) -> list[Path]:
    if raw_paths:
        return [Path(raw).resolve() for raw in raw_paths]
    return sorted(REPO_ROOT.glob(DEFAULT_GLOB))


def main() -> int:
    args = parse_args()
    paths = resolve_paths(args.paths)
    if not paths:
        print(f"FAIL: 未找到待校验文件：{DEFAULT_GLOB}")
        return 2

    total_failures = 0
    total_warnings = 0
    for path in paths:
        validator = Validator(path)
        issues = validator.validate()
        failures = [issue for issue in issues if issue.level == "FAIL"]
        warnings = [issue for issue in issues if issue.level == "WARN"]
        total_failures += len(failures)
        total_warnings += len(warnings)

        rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        if not args.quiet or failures:
            print(f"\n=== {rel} ===")
            if issues:
                for issue in issues:
                    print(f"{issue.level}: {issue.path}: {issue.message}")
            else:
                print(f"PASS: {validator.test_point_count} 个测试点，{validator.step_count} 个步骤，{len(validator.ids)} 个节点 id 全部通过")

    print(f"\n=== 汇总 ===")
    print(f"文件数：{len(paths)}")
    print(f"失败：{total_failures}")
    print(f"警告：{total_warnings}")
    return 1 if total_failures else 0


if __name__ == "__main__":
    sys.exit(main())
