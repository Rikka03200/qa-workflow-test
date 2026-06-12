#!/usr/bin/env python
"""
scripts/batch_generate.py

Phase 2 · 弱生成环（多模型协作）。
驱动拼装上下文（项目契约 + JSON schema + 用例规范 + 04/05 生成步规格 +
CodeArts 目录树 + 本工单已就绪的工件）→ 调廉价模型（百炼 qwen）一次性生成
完整 test-design.json → 跑 validate-test-design.py 确定性校验 → 失败把 FAIL 行
喂回重生成，循环到通过或用尽轮次。

分工定位：强模型（主 Claude）已前置产出 analysis.md / test-points.md 等判断层工件；
本脚本是“弱扩展”——把已确认的测试点机械展开为合法 JSON。校验器是免费硬闸门，
真正的语义/算术/覆盖终检仍由主 Claude 抽检（Phase 3）。

单工单用法：
  python scripts/batch_generate.py tickets/wms/2026-06-02/EAR-240953 \
      --out test-design.gen.json --rounds 3
  # 正式写入 test-design.json 需显式 --force（防止覆盖人工评审过的产物）

作为库：
  from batch_generate import build_ticket
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from cheap_model import generate  # noqa: E402
try:
    import kb_store  # noqa: E402
except Exception:  # noqa: BLE001
    kb_store = None

# 工单工件（用户内容）：按此顺序拼装，缺失则跳过
TICKET_INPUTS = [
    "requirement.md",
    "business-context.md",
    "linked-issues.md",
    "analysis.md",
    "questions.md",
    "test-points.md",
]


def _read(p: Path) -> str:
    try:
        rel = p.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel = ""
    if kb_store is not None and rel.startswith("_kb/"):
        content = kb_store.read_text(rel, p)
        if content:
            return content
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def build_system(product: str) -> str:
    """拼装系统提示：项目契约 + 全局 schema + 产品目录树/样式 + 04/05 生成规格。"""
    spec_files = [
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / "_kb" / "_global" / "codearts-json-schema.md",
        REPO_ROOT / "_kb" / "_global" / "case-writing-spec.md",
        REPO_ROOT / "_kb" / "projects" / product / "modules.md",
        REPO_ROOT / "_kb" / "projects" / product / "case-samples" / "style-notes.md",
        REPO_ROOT / "prompts" / "04-skeleton.md",
        REPO_ROOT / "prompts" / "05-cases-detail.md",
    ]
    blocks = [
        "你是资深软件测试工程师（QA）。下面依次给出：项目契约（CLAUDE.md）、"
        "CodeArts JSON 树 schema、用例编写规范、本产品的 CodeArts 目录树与命名样式、"
        "以及生成步 04/05 的规格。请把它们当作必须严格遵守的硬规则。\n\n"
        "你当前只做【生成步】（把 skeleton 与 detail 合并为一次产出），"
        "不需要调用任何工具或 MCP——所有需要的输入都已在用户消息里给全。\n"
        "唯一任务：基于用户给出的工单工件，产出完整、合法、纯 JSON 的 test-design.json。\n"
        "【输出格式铁律】只输出 JSON 数组本身；不要任何解释、不要 Markdown 代码块围栏、"
        "不要自查报告、不要前后缀文字。\n"
        "【容器路径铁律】测试点前的容器节点只能取自 modules.md §2 CodeArts 目录树；"
        "若同名节点在 §2 多个分支出现（例：“波次策略”同时挂在“出库操作”和“业务规则”下），"
        "以 §2.1「真实」二级菜单的归属为准（如 波次策略 → 标品管理/业务规则）；"
        "仓配App 的 集货/容器拆并/复核/拣货 等按 §2 仓配app 下的实际同级节点归属，勿凭系统菜单层级反推；"
        "§2 中 仓配app 只到功能层一级（如 仓配app>容器拆并），不要再往下加 容器合并 等子容器——"
        "§4「操作模块树」的层级不是 CodeArts 存放路径，测试点直接挂在功能层容器下。\n"
        "【UI 文案/元素】按钮、弹窗、标题、菜单项等 UI 元素用功能性描述直接写，不纠结精确文案、不标 [待确认]（产品常未定、最终由开发定，执行者看懂即可）——如“点击打印”“显示预生成集货托盘弹窗”“弹出个数输入弹窗”。"
        "【抗幻觉·仅限行为/规则】[待确认] 只留给业务行为或规则本身不确定处（某分支是否走某规则、状态如何流转、某校验是否存在），不要用来等 UI 文案。方案/规则已给出的确切提示文案（如“未找到托盘号！”“托盘号不存在!”）照写入 expect。"
        "【questions.md 答案优先】如果 questions.md 的“✅ 答案”已有非注释内容，必须按答案消解对应不确定点；禁止在同一规则上继续输出 [待确认]。用户答案写“先不考虑/不覆盖”的场景，不生成对应测试点或在不覆盖范围体现，不要在 JSON 里保留待确认。"
        "【L1 才能写确定值】expect/step 里的业务规则、状态流转、提示文案、字段联动，确定值只能来自 L1 修改方案或已确认的 questions.md 答案。关联工单评论/客户报障/分析中的“候选答案”属 L2/L3，禁止当作确定 expect 写入；这类点要么不写、要么标 [待确认: 简述]，绝不把 L2 推断固化成确定结果。",
    ]
    for f in spec_files:
        content = _read(f)
        if content:
            blocks.append(f"\n\n===== {f.relative_to(REPO_ROOT).as_posix()} =====\n{content}")
    return "".join(blocks)


def select_gold(ticket_dir: Path, product: str, max_n: int = 1) -> list[Path]:
    """按模块相关度挑选最贴近的黄金范例（few-shot）。无 gold 池则返回空（机制休眠，不影响现状）。"""
    gold_dir = REPO_ROOT / "_kb" / "projects" / product / "case-samples" / "gold"
    if not gold_dir.exists():
        return []
    golds = list(gold_dir.glob("*.json"))
    if not golds:
        return []
    cur = " ".join(_read(ticket_dir / n) for n in ("requirement.md", "analysis.md", "test-points.md"))
    cur_ear = ticket_dir.name
    scored: list[tuple[int, float, Path]] = []
    for g in golds:
        if g.stem == cur_ear:          # 不拿自己当范例
            continue
        try:
            data = json.loads(g.read_text(encoding="utf-8"))
        except Exception:
            continue
        words: set[str] = set()

        def collect(n):
            if isinstance(n, list):
                for x in n:
                    collect(x)
            elif isinstance(n, dict):
                if not any(k in n for k in ("mark", "testPoint", "step", "expect", "condition")):
                    t = str(n.get("text", "")).strip()
                    if t:
                        words.add(t)
                for c in n.get("children", []) or []:
                    collect(c)

        collect(data)
        score = sum(1 for w in words if w and w in cur)   # 容器/模块名在本工单线索里的命中数
        scored.append((score, g.stat().st_mtime, g))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    picked = [g for s, m, g in scored if s > 0][:max_n]
    if not picked and scored:          # 无模块重叠时，退一个最近的作通用风格范例
        picked = [scored[0][2]]
    return picked


def build_user(ticket_dir: Path, ear: str) -> str:
    blocks = [
        f"工单号：{ear}\n以下是本工单已就绪的工程工件（强模型已完成需求理解/上下文/分析/测试点判断）。"
        f"请据此把测试点机械展开为完整 test-design.json。\n"
    ]
    for name in TICKET_INPUTS:
        content = _read(ticket_dir / name)
        if content:
            blocks.append(f"\n\n===== {name} =====\n{content}")
    # 黄金范例（few-shot）：已评审定稿的相近工单，供 expect 写法/粒度/风格参考（无 gold 池则跳过）
    product = ticket_dir.parent.parent.name
    for g in select_gold(ticket_dir, product):
        blocks.append(
            f"\n\n===== 参考范例 {g.stem}（已评审定稿，仅供风格/粒度/expect 写法参考；"
            f"禁止照抄其容器路径/测试点/数值/文案，本工单内容只能来自上面的工件）=====\n{_read(g)}"
        )
    blocks.append(
        f"\n\n===== 现在生成 =====\n"
        f"为工单 {ear} 产出完整 test-design.json：单根树（根 text=\"{ear}\", side=\"right\"）；"
        f"容器路径只来自上面的 modules.md §2 目录树；每个测试点含 mark.priority + testPoint.id；"
        f"每个 step 恰好 1 个 expect 子节点；前置条件（如有）置于测试点 children 第一位且无 children；"
        f"所有 id 与 testPoint.id 为 32 位大写十六进制、全文件唯一；节点 text 自包含（禁工单号/来源/章节/附件名）；"
        f"强调用中文双引号“”，禁 <b>/<strong>/反引号/HTML 实体。只输出 JSON。"
    )
    return "".join(blocks)


FIX_PREAMBLE = (
    "\n\n===== 上一轮校验未通过，请修复 =====\n"
    "你上一轮生成的 test-design.json 未通过确定性校验。请【只修复】下列问题，"
    "重新输出【完整】的 JSON（不要省略未改动部分，不要任何解释）：\n\n"
    "【校验失败项】\n{fails}\n\n"
    "【上一轮 JSON】\n{prev}\n"
)


def extract_json(text: str) -> str:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S | re.I)
    if m:
        t = m.group(1).strip()
    # 优先：整体已是合法 JSON 数组就直接用（避免下面截首尾括号误伤对象根 / 含括号的说明文字）
    try:
        if isinstance(json.loads(t), list):
            return t
    except Exception:
        pass
    # 回退：从首个 '[' 到末个 ']'（容忍前后少量说明文字）
    i, j = t.find("["), t.rfind("]")
    if i != -1 and j != -1 and j > i:
        t = t[i : j + 1]
    return t


def count_stats(data) -> dict:
    stats = {"testpoints": 0, "steps": 0, "expects": 0, "conditions": 0, "todo": 0, "nodes": 0}

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        stats["nodes"] += 1
        if "mark" in node or "testPoint" in node:
            stats["testpoints"] += 1
        if node.get("step") == "Y":
            stats["steps"] += 1
        if node.get("expect") == "Y":
            stats["expects"] += 1
        if node.get("condition") == "Y":
            stats["conditions"] += 1
        if "[待确认" in str(node.get("text", "")):
            stats["todo"] += 1
        for c in node.get("children", []) or []:
            walk(c)

    walk(data)
    return stats


def run_validator(script: str, *args: str) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def build_ticket(
    ticket_dir: Path,
    out_name: str = "test-design.json",
    rounds: int = 3,
    max_tokens: int | None = None,
    force: bool = False,
) -> dict:
    ticket_dir = ticket_dir.resolve()
    ear = ticket_dir.name
    product = ticket_dir.parent.parent.name
    out_path = ticket_dir / out_name

    # 防覆盖：除非 --force，不写非空的 test-design.json（保护人工评审产物）
    if out_name == "test-design.json" and out_path.exists():
        if out_path.stat().st_size > 5 and not force:
            return {
                "ear": ear,
                "ok": False,
                "error": f"{out_path} 已存在且非空；如确需覆盖请加 --force，"
                f"或用 --out test-design.gen.json 生成到旁路文件",
            }

    # force 覆盖正式产物前先备份上一版，避免误覆盖人工/强模型已评审的 test-design.json（不可逆）
    if out_name == "test-design.json" and force and out_path.exists() and out_path.stat().st_size > 5:
        try:
            bak = out_path.with_name(out_path.name + ".bak")
            bak.write_text(out_path.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"[{ear}] 已备份上一版 → {bak.name}")
        except Exception:
            pass

    system = build_system(product)
    base_user = build_user(ticket_dir, ear)
    print(f"[{ear}] product={product}  system≈{len(system)//1000}KB  user≈{len(base_user)//1000}KB")

    fix_block = ""
    in_tok = out_tok = 0
    t0 = time.time()
    for rnd in range(1, rounds + 1):
        user = base_user + fix_block
        try:
            text, msg = generate(system=system, user=user, max_tokens=max_tokens)
        except Exception as e:
            print(f"[{ear}] 第{rnd}轮 模型调用失败：{type(e).__name__}: {e}")
            return {"ear": ear, "ok": False, "error": f"model call failed: {e}"}

        usage = getattr(msg, "usage", None)
        if usage:
            # 含 prompt 缓存的读/写 token，否则启用缓存后 in_tok 会被低估
            in_tok += (getattr(usage, "input_tokens", 0) or 0)
            in_tok += (getattr(usage, "cache_read_input_tokens", 0) or 0)
            in_tok += (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            out_tok += getattr(usage, "output_tokens", 0) or 0

        raw = extract_json(text)
        try:
            data = json.loads(raw)
        except Exception as e:
            print(f"[{ear}] 第{rnd}轮 输出非合法 JSON：{e}（回退重生成）")
            fix_block = FIX_PREAMBLE.format(fails=f"输出不是合法 JSON：{e}", prev=raw[:3000])
            continue

        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rc, vout = run_validator("validate-test-design.py", str(out_path))
        rcc, cout = run_validator("validate-containers.py", str(out_path), "--product", product, "--quiet")
        fails = [ln for ln in (vout + cout).splitlines() if ln.startswith("FAIL:")]
        warns = [ln for ln in (vout + cout).splitlines() if ln.startswith("WARN:")]
        st = count_stats(data)
        print(
            f"[{ear}] 第{rnd}轮 → 测试点{st['testpoints']} step{st['steps']} "
            f"expect{st['expects']} 待确认{st['todo']} | 结构rc={rc} 容器rc={rcc} "
            f"FAIL={len(fails)} WARN={len(warns)}"
        )

        # rc/rcc==2 = 校验器“无法校验”（缺 modules.md/§2、文件损坏等环境问题）；
        # 或非零退出却没有任何可喂回的 FAIL 行（异常崩溃）——都不是模型能修的，硬中止本单，
        # 避免空反馈 / 不可修复反馈空烧 rounds。
        if rc == 2 or rcc == 2 or ((rc != 0 or rcc != 0) and not fails):
            dt = time.time() - t0
            infra = ((vout + cout).strip() or "(无输出)")[:1500]
            print(f"[{ear}] ⛔ 校验器基础设施错误（结构rc={rc} 容器rc={rcc}，无可喂回 FAIL，已中止本单）：\n{infra}")
            return {"ear": ear, "ok": False, "error": "validator infra error",
                    "rc": rc, "rcc": rcc, "out": str(out_path), "seconds": round(dt)}

        # 通过条件：结构校验 0 且容器校验 0（容器仅 WARN 时 rcc=0，不阻断；
        # 容器错挂 rcc=1 会进入下面的 FAIL 回喂，由弱模型据“合法父级”提示自纠）
        if rc == 0 and rcc == 0:
            dt = time.time() - t0
            print(f"[{ear}] ✅ 通过校验（{rnd}轮，{dt:.0f}s，in={in_tok}/out={out_tok} token）→ {out_path.name}")
            return {
                "ear": ear, "ok": True, "rounds": rnd, "stats": st,
                "out": str(out_path), "in_tok": in_tok, "out_tok": out_tok,
                "warns": len(warns), "seconds": round(dt),
            }
        for ln in fails[:12]:
            print(f"        {ln}")
        fix_block = FIX_PREAMBLE.format(fails="\n".join(fails), prev=json.dumps(data, ensure_ascii=False)[:8000])

    dt = time.time() - t0
    print(f"[{ear}] ❌ {rounds}轮仍未通过校验（{dt:.0f}s）→ {out_path.name}（保留最后一轮供排查）")
    return {"ear": ear, "ok": False, "rounds": rounds, "out": str(out_path), "seconds": round(dt)}


def main() -> int:
    ap = argparse.ArgumentParser(description="弱模型生成 test-design.json + 校验闭环")
    ap.add_argument("ticket_dir", help="工单目录，如 tickets/wms/2026-06-02/EAR-240953")
    ap.add_argument("--out", default="test-design.json", help="输出文件名（测试用 test-design.gen.json 避免覆盖）")
    ap.add_argument("--rounds", type=int, default=3, help="最多重生成轮次（默认 3）")
    ap.add_argument("--max-tokens", type=int, default=None, help="单次输出上限（默认取 config）")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的 test-design.json")
    args = ap.parse_args()

    td = Path(args.ticket_dir)
    if not td.exists():
        print(f"工单目录不存在：{td}")
        return 2
    res = build_ticket(td, out_name=args.out, rounds=args.rounds, max_tokens=args.max_tokens, force=args.force)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
