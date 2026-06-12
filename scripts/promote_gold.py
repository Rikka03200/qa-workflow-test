#!/usr/bin/env python
"""
scripts/promote_gold.py

把一份【已人工评审定稿】的工单 test-design.json 提升为「黄金范例」，
存入 _kb/projects/<product>/case-samples/gold/，供弱模型生成时作 few-shot 参考。
闸门：只有通过全部校验器（结构 + 产物 + 容器）的产物才允许提升——避免把坏样例当范本。

用法：
  python scripts/promote_gold.py tickets/wms/2026-05-12/EAR-240883 --note "预生成集货托盘：跨 web+app 4 部分，状态机+合并继承"
  python scripts/promote_gold.py <工单目录> [--product wms] [--note "此单属哪类/为何是好范例"] [--force]
退出码：0=已提升；1=未过校验/已存在；2=找不到文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import batch_generate as bg  # noqa: E402  (复用 run_validator / count_stats)


def main() -> int:
    ap = argparse.ArgumentParser(description="把评审定稿的 test-design.json 提升为黄金范例（few-shot）")
    ap.add_argument("ticket_dir", help="工单目录，如 tickets/wms/2026-05-12/EAR-240883")
    ap.add_argument("--product", default=None, help="默认从路径推断（tickets/<product>/...）")
    ap.add_argument("--note", default="", help="一句话：此单属哪类 / 为何是好范例")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的同名范例")
    a = ap.parse_args()

    td_dir = Path(a.ticket_dir)
    if not td_dir.is_absolute():
        td_dir = REPO_ROOT / td_dir
    ear = td_dir.name
    product = a.product or td_dir.parent.parent.name
    td = td_dir / "test-design.json"
    if not td.exists():
        print(f"FAIL: 找不到 {td}")
        return 2

    # 闸门：必须过全部校验器才允许作为范例
    checks = [
        ("validate-test-design.py", bg.run_validator("validate-test-design.py", str(td))),
        ("check-ticket-artifacts.py", bg.run_validator("check-ticket-artifacts.py", str(td_dir))),
        ("validate-containers.py", bg.run_validator("validate-containers.py", str(td), "--product", product, "--quiet")),
    ]
    bad = [(n, rc, out) for n, (rc, out) in checks if rc != 0]
    if bad:
        print(f"FAIL: {ear} 未通过全部校验，不能作为黄金范例：")
        for n, rc, out in bad:
            print(f"  - {n} exit={rc}")
            for ln in out.splitlines():
                if ln.startswith("FAIL:"):
                    print(f"      {ln}")
        return 1

    gold_dir = REPO_ROOT / "_kb" / "projects" / product / "case-samples" / "gold"
    gold_dir.mkdir(parents=True, exist_ok=True)
    dest = gold_dir / f"{ear}.json"
    if dest.exists() and not a.force:
        print(f"已存在 {dest.name}；如需更新加 --force")
        return 1
    dest.write_text(td.read_text(encoding="utf-8"), encoding="utf-8")

    # 统计 + 维护 README 索引（按工单号 upsert，重排）
    st = bg.count_stats(json.loads(td.read_text(encoding="utf-8")))
    readme = gold_dir / "README.md"
    header = [
        "# 黄金范例（已评审定稿，供弱模型 few-shot 参考）",
        "",
        "> 由 `scripts/promote_gold.py` 维护；`batch_generate` 生成时按模块相关度自动挑选最贴近的范例喂给弱模型。",
        "> 只放**通过全部校验、且你人工评审认可**的产物。",
        "",
        "| 工单 | 测试点 | step | 说明 |",
        "|---|---|---|---|",
    ]
    rows: dict[str, str] = {}
    if readme.exists():
        for ln in readme.read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s.startswith("| ") and "---" not in s and "工单" not in s:
                first = s.strip("|").split("|")[0].strip()
                if first:
                    rows[first] = s
    rows[ear] = f"| {ear} | {st['testpoints']} | {st['steps']} | {a.note or '(未填说明)'} |"
    readme.write_text("\n".join(header + [rows[k] for k in sorted(rows)]) + "\n", encoding="utf-8")

    print(f"✅ 已提升为黄金范例：{dest.relative_to(REPO_ROOT).as_posix()}（{st['testpoints']} 测试点 / {st['steps']} step）")
    print(f"   索引：{readme.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
