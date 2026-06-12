#!/usr/bin/env python
"""
scripts/run_sprint.py

Sprint 级批量驱动（多模型协作）：对多个工单【并行】跑 qa_pipeline（弱链 → 校验 → QA 包），
带【断点续跑】（progress.json）与【汇总看板】。强模型（主 Claude）随后逐单读 _qa-packet.md 抽检。

用法：
  python scripts/run_sprint.py --product wms --keys EAR-240883,EAR-242289
  python scripts/run_sprint.py --product wms --sprint 2026-05-12      # 重跑该 sprint 下已有目录
  python scripts/run_sprint.py --product wms --keys EAR-1,EAR-2 --concurrency 3 --force
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
# 工单根目录：可被 QA_TICKETS_ROOT 覆盖（webapp 按用户注入独立目录）；默认仓库 tickets/。
TICKETS_ROOT = Path(os.environ.get("QA_TICKETS_ROOT") or (REPO_ROOT / "tickets"))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import qa_pipeline as qp  # noqa: E402
import batch_generate as bg  # noqa: E402
import select_sprint as ss  # noqa: E402
from core.productcfg import DEFAULT_PRODUCT, get_product  # noqa: E402


def display_path(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        try:
            return os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")
        except ValueError:
            return str(path)


def list_keys_from_sprint(product: str, sprint: str) -> list[str]:
    base = TICKETS_ROOT / product / sprint
    if not base.exists():
        return []
    pc = get_product(product)
    return sorted(p.name for p in base.glob(pc.ticket_glob()) if p.is_dir() and pc.valid_ticket_key(p.name))


def find_dir(product: str, key: str) -> Path | None:
    dirs = list((TICKETS_ROOT /product).glob(f"*/{key}"))
    return dirs[0] if dirs else None


def ticket_stats(ticket_dir: Path) -> tuple[int, int, dict]:
    td = ticket_dir / "test-design.json"
    rc1, _ = bg.run_validator("validate-test-design.py", str(td))
    rc2, _ = bg.run_validator("check-ticket-artifacts.py", str(ticket_dir))
    st = {"testpoints": 0, "steps": 0, "expects": 0, "todo": 0}
    try:
        st = bg.count_stats(json.loads(td.read_text(encoding="utf-8")))
    except Exception:
        pass
    return rc1, rc2, st


def run_one(key: str, product: str, sprint: str | None, rounds: int,
            only: list[str] | None = None, redo: list[str] | None = None) -> dict:
    t0 = time.time()
    try:
        res = qp.run_pipeline(key, product, sprint, only=only, redo=redo, rounds=rounds)
        # design 生成失败（校验未过 / 轮次耗尽）不会抛异常，必须显式检查；
        # 否则该单会被误记为“完成”、写进断点、下次永久跳过，吐出未经校验的产物。
        # --until draft 这类只产草稿的分段无 design，以 draft 成败判定本轮。
        d = res.get("design")
        if d is None:
            d = res.get("draft")
        design_ok = (d is None) or bool(d.get("ok"))
        return {"key": key, "ok": design_ok,
                "err": None if design_ok else "design 校验未通过（产物已留存，下次续跑会重试）",
                "dir": res.get("dir"),
                "log": " ".join(res.get("log", [])), "sec": round(time.time() - t0)}
    except SystemExit as e:
        return {"key": key, "ok": False, "err": str(e), "sec": round(time.time() - t0)}
    except Exception as e:
        return {"key": key, "ok": False, "err": f"{type(e).__name__}: {e}", "sec": round(time.time() - t0)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Sprint 并行批量驱动 + 断点续跑 + 看板")
    ap.add_argument("--product", default=DEFAULT_PRODUCT)
    ap.add_argument("--keys", default=None, help="工单号逗号分隔（与 --sprint 二选一或并用）")
    ap.add_argument("--sprint", default=None, help="Sprint 日期目录（重跑其下已有工单）")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--force", action="store_true", help="忽略 progress.json，全部重跑")
    ap.add_argument("--only", default=None, help="透传给 qa_pipeline：只跑这些步骤（逗号分隔）")
    ap.add_argument("--redo", default=None, help="透传给 qa_pipeline：强制重做这些步骤（逗号分隔）")
    ap.add_argument("--resume-after-questions", action="store_true",
                    help="questions.md 已人工回答后继续：保留前序产物，只重做 points/design/packet")
    ap.add_argument("--until", default=None, metavar="STEP",
                    help="只生成到该步骤为止（含）。--until draft：生成 fetch..questions..points..draft（草稿用例），"
                         "停在强模型复核+人工回答前；--until questions：更早停在 qa:resolve/人工回答前")
    ap.add_argument("--fresh", action="store_true",
                    help="从头重生成：范围内全部阶段强制重做（重抓 Jira + 重生成），并清掉范围之后的下游产物（回到该阶段末态）")
    ap.add_argument("--select", action="store_true",
                    help="按选单规则自动选取本 sprint 工单（需 --sprint 日期）：测试员=配置 ∧ 提高 ∧ 未解决 + 拆单/主单去重 + 跨 sprint 覆盖")
    ap.add_argument("--dry-run", action="store_true", help="配合 --select：只算选单+写报告，不驱动弱模型生成")
    ap.add_argument("--board", default=None, help="看板 id（--select 解析 sprint 用；默认读 config / 本地 rapidViewId）")
    a = ap.parse_args()
    only = [s.strip() for s in a.only.split(",") if s.strip()] if a.only else None
    redo = [s.strip() for s in a.redo.split(",") if s.strip()] if a.redo else None
    if a.until:
        if a.until not in qp.PIPELINE_ORDER:
            print(f"--until 步骤名非法：{a.until}（可选：{','.join(qp.PIPELINE_ORDER)}）")
            return 2
        until_only = qp.PIPELINE_ORDER[: qp.PIPELINE_ORDER.index(a.until) + 1]
        only = until_only if only is None else [s for s in only if s in until_only]
    if a.resume_after_questions:
        if only is None:
            only = ["points", "design", "packet"]
        if redo is None:
            redo = ["points", "design", "packet"]
    if a.fresh:
        redo = list(only or qp.PIPELINE_ORDER)  # 从头重生成：范围内全部阶段强制重做（含 fetch 重抓 Jira）
    # 本轮是否会真正生成 design——决定是否记“完成/覆盖”（分段 --until questions 不含 design，不记账）
    design_in_run = (only is None) or ("design" in only)

    run_meta: dict[str, dict] = {}
    if a.select:
        if not a.sprint:
            print("--select 需要 --sprint <日期>（如 --sprint 2026-06-09）")
            return 2
        p = ss.plan(a.product, a.sprint, board=a.board)
        out = ss.write_reports(p)
        run_meta = {d["key"]: d for d in p["decisions"] if d["decision"] == "run"}
        keys = list(p["run_list"])
        print(f"选单：JQL命中 {p.get('jql_total','?')} → 候选(提高∧测试员∧未解决) {p['candidate_count']} "
              f"→ 运行 {len(keys)} / 跳过 {p['candidate_count'] - len(keys)}　"
              f"报告 → {display_path(Path(out['md']))}")
        if p.get("review_needed"):
            print(f"⚠️ {len(p['review_needed'])} 个拆单未匹配到主单，按独立单处理，请看报告人工复核。")
        if a.dry_run:
            print("（--dry-run：仅选单，不生成）\n运行清单：" + (",".join(keys) or "（空）"))
            return 0
        if not keys:
            print("选单结果为空：本 sprint 无需生成（可能全部已走过或无符合条件工单）。")
            return 0
    else:
        keys = []
        if a.keys:
            keys += [k.strip() for k in a.keys.split(",") if k.strip()]
        # --keys 显式给定时它就是权威清单（单工单操作）；--sprint 仅用于状态目录标签，
        # 不再把整个 sprint 目录的工单并进来（否则单工单操作会误跑整 sprint）。
        if a.sprint and not a.keys:
            keys += [k for k in list_keys_from_sprint(a.product, a.sprint) if k not in keys]
        if not keys:
            print("无工单：请给 --keys 或 --sprint（或加 --select 自动选单）")
            return 2

    # 运行态文件（断点 + 看板）收进隐藏子目录，不污染 tickets/<product>/ 根视图
    state_dir = TICKETS_ROOT /a.product / ".sprint-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    tag = ("-" + a.sprint) if a.sprint else ""
    prog_path = state_dir / f"_sprint-progress{tag}.json"
    done: set[str] = set()
    if prog_path.exists() and not a.force and not a.resume_after_questions:
        try:
            done = set(json.loads(prog_path.read_text(encoding="utf-8")).get("done", []))
        except Exception as e:
            print(f"⚠️ 断点文件损坏，无法读取（{e}）：本次按“无完成记录”处理，将重跑全部工单。"
                  f"\n   若不想重跑，请先修复或删除 {prog_path.name} 后再运行。\n")
    todo = [k for k in keys if k not in done]
    if a.fresh:
        # 重新生成（--fresh）= 从头重跑：必须把本次范围内的工单从“已完成”断点账本里重置，
        # 否则它们会被 done 直接跳过（控制台显示“已完成跳过 N，本次跑 0”），达不到从头重跑的语义。
        # （--until questions 阶段不重新登记 design，故重置后工单真正回到「待确认」。）
        reset = set(done) & set(keys)
        if reset:
            done -= reset
            tmp = prog_path.with_name(prog_path.name + ".tmp")
            tmp.write_text(json.dumps({"done": sorted(done)}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, prog_path)  # 原子写，落地重置后的账本
            print(f"重新生成：已重置 {len(reset)} 单的完成记录（{','.join(sorted(reset))}），将从头重跑。")
        todo = [k for k in keys if k not in done]
        if only is not None:
            # 从头重生成：清掉本范围之后的下游产物，让工单干净回到本范围末态
            # （如 --until questions 时清掉 points/design/packet，回到「待确认」步骤）。design 备份成 .bak。
            scope = set(only)
            for k in todo:
                d = find_dir(a.product, k)
                if not d:
                    continue
                for st, fn in (("points", "test-points.md"), ("draft", "_draft-design.json"),
                               ("design", "test-design.json"), ("packet", "_qa-packet.md")):
                    if st in scope:
                        continue
                    fp = d / fn
                    if not fp.exists():
                        continue
                    try:
                        if fn == "test-design.json":
                            fp.replace(fp.with_name(fp.name + ".bak"))  # 备份后移除
                        else:
                            fp.unlink()
                    except OSError:
                        pass
    mode = "questions 已回答续跑" if a.resume_after_questions else "Sprint 批量"
    print(f"{mode}：共 {len(keys)} 单，已完成跳过 {len(done)}，本次跑 {len(todo)}（并发 {a.concurrency}）\n")

    results: list[dict] = []
    if todo:
        with cf.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            futs = {ex.submit(run_one, k, a.product, a.sprint, a.rounds, only, redo): k for k in todo}
            for fut in cf.as_completed(futs):
                r = fut.result()
                results.append(r)
                mark = "✅" if r["ok"] else "❌"
                print(f"  {mark} {r['key']} ({r['sec']}s) {r.get('log') or ''} {r.get('err') or ''}")
                if r["ok"]:
                    # 只有本轮确实生成了 design（完整生成或 resume 续跑）才记“完成/覆盖”；
                    # --until questions 这类“生成到一半”的分段不得提前记账，否则账本误判已覆盖、
                    # 将来真正 sprint 会把它跳过，且断点会让 design 永不生成。
                    if design_in_run:
                        done.add(r["key"])
                        # 原子写：先写临时文件再 os.replace，避免进程被杀时断点文件被截断清空
                        tmp = prog_path.with_name(prog_path.name + ".tmp")
                        tmp.write_text(json.dumps({"done": sorted(done)}, ensure_ascii=False, indent=2), encoding="utf-8")
                        os.replace(tmp, prog_path)
                        # 选单模式：成功生成即登记覆盖（该功能特性以后在主单所在 sprint 自动跳过）。
                        # 含 resume：分段编排步骤5 用 `--select --resume-after-questions`，design 在此轮才生成，
                        # 必须在此记账，否则 coverage-ledger 永远拿不到 source=run 记录（§4.5.3 去重失效）。
                        if a.select and r["key"] in run_meta:
                            m = run_meta[r["key"]]
                            ss.mark_covered(a.product, m["feature"], r["key"], a.sprint,
                                            platform=m.get("platform"), source="run")

    # 汇总看板
    rows = []
    json_ok = True
    for k in keys:
        d = find_dir(a.product, k)
        if not d:
            rows.append((k, "-", "-", "-", "-", "无目录"))
            continue
        rc1, rc2, st = ticket_stats(d)
        if rc1 != 0:
            json_ok = False
        rows.append((k, "PASS" if rc1 == 0 else f"FAIL({rc1})", "PASS" if rc2 == 0 else f"FAIL({rc2})",
                     st.get("testpoints", 0), st.get("todo", 0), display_path(d)))
    lines = [
        "# Sprint 批量汇总看板", "",
        f"产品 `{a.product}` / sprint `{a.sprint or '(指定 keys)'}` · 共 {len(keys)} 单", "",
        "| 工单 | JSON校验 | 产物校验 | 测试点 | 待确认 | 目录 |",
        "|---|---|---|---|---|---|",
    ]
    for k, v, c, tp, td, dd in rows:
        lines.append(f"| {k} | {v} | {c} | {tp} | {td} | {dd} |")
    lines += ["", "> 强模型抽检：逐单读 `<目录>/_qa-packet.md`，重点核范围/覆盖/算术/L2L3 误入。"]
    summary = "\n".join(lines) + "\n"
    out = state_dir / f"_sprint-summary{tag}.md"
    out.write_text(summary, encoding="utf-8")
    print("\n" + summary)
    print(f"看板 → {display_path(out)}")
    # 退出码：以「本轮运行是否成功」(run_ok) + 正式用例 JSON 是否可交付为准。
    # 旧的 check-ticket-artifacts.py 会检查 README/requirement/analysis 等中间产物格式；这些问题仍写进
    # summary 的「产物校验」列供排查，但不能把已经产出且 JSON 合法的 test-design.json 误标成生成失败。
    run_ok = all(r["ok"] for r in results)
    ok = run_ok and (json_ok or not design_in_run)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
