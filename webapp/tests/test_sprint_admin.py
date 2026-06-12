"""新增交互回归：删除 Sprint（回收站 + 状态/账本/归属清理）+ 重新生成模式。"""

import asyncio
import json
import sys

import pytest

from webapp import config
from webapp.services import ownership, selection


def _seed(tmp_path, monkeypatch):
    """造一个最小的 tickets/ 工作树：wms/2026-06-16 有 2 单 + 状态文件 + 账本。"""
    monkeypatch.setattr(config, "TICKETS_DIR", tmp_path)  # tickets_root() 无会话时回退到它
    base = tmp_path / "wms"
    for ear in ("EAR-1", "EAR-2"):
        d = base / "2026-06-16" / ear
        d.mkdir(parents=True)
        (d / "test-design.json").write_text("[]", encoding="utf-8")
    sd = base / ".sprint-state"
    sd.mkdir(parents=True)
    (sd / "_selection-2026-06-16.json").write_text("{}", encoding="utf-8")
    (sd / "_selection-2026-06-16.md").write_text("# x", encoding="utf-8")
    (sd / "_sprint-progress-2026-06-16.json").write_text('{"done":[]}', encoding="utf-8")
    # 账本：一条本期覆盖 + 一条其它 sprint 覆盖（后者必须保留）
    (sd / "coverage-ledger.json").write_text(json.dumps({"features": {
        "EAR-1": {"covered_by": "EAR-1", "sprint": "2026-06-16", "source": "run"},
        "EAR-9": {"covered_by": "EAR-9", "sprint": "2026-05-01", "source": "scan"},
    }}, ensure_ascii=False), encoding="utf-8")
    return base


def test_delete_sprint_trashes_and_cleans(tmp_path, monkeypatch):
    base = _seed(tmp_path, monkeypatch)
    info = selection.delete_sprint("wms", "2026-06-16")

    # 1) 工单产物移入回收站（原目录消失，回收站里完整保留——可恢复，不硬删）
    assert not (base / "2026-06-16").exists()
    assert info["tickets"] == 2
    trash = base / ".trash"
    moved = list(trash.glob("2026-06-16-*/EAR-1/test-design.json"))
    assert moved, "回收站里应能找回工单产物"

    # 2) 该日期的运行态文件被清掉
    sd = base / ".sprint-state"
    assert not (sd / "_selection-2026-06-16.json").exists()
    assert not (sd / "_sprint-progress-2026-06-16.json").exists()
    assert info["state_files"] >= 3

    # 3) 账本：本期覆盖的特性移除，其它 sprint 的保留
    led = json.loads((sd / "coverage-ledger.json").read_text(encoding="utf-8"))
    assert "EAR-1" not in led["features"] and "EAR-9" in led["features"]
    assert info["ledger_removed"] == 1


def test_delete_empty_sprint_no_error(tmp_path, monkeypatch):
    """未同步（无产物/无状态）的 Sprint 也能删：返回 0 单、无回收站项，不报错。"""
    monkeypatch.setattr(config, "TICKETS_DIR", tmp_path)
    info = selection.delete_sprint("wms", "2026-07-01")
    assert info["tickets"] == 0 and info["trashed"] is None


def test_delete_sprint_rejects_bad_params(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TICKETS_DIR", tmp_path)
    with pytest.raises(ValueError):
        selection.delete_sprint("wms", "../etc")          # 非法日期 → 拒
    with pytest.raises(ValueError):
        selection.delete_sprint("../x", "2026-06-16")     # 非法产品 → 拒


def test_ownership_remove(tmp_path, monkeypatch):
    monkeypatch.setattr(ownership, "_LEDGER", tmp_path / "own.json")
    ownership.set_owner("wms", "2026-06-16", "alice")
    ownership.set_owner("wms", "2026-06-23", "alice")
    ownership.remove("wms", "2026-06-16")
    assert ownership.owner_of("wms", "2026-06-16") is None
    assert ownership.owner_of("wms", "2026-06-23") == "alice"   # 同产品其它 sprint 不受影响
    ownership.remove("wms", "2026-06-23")
    assert ownership._load().get("wms") is None                 # 产品下清空则删掉空字典


def test_regen_mode_args_and_label():
    from webapp.routers import jobs
    assert "regen" in jobs._MODES and "draft" in jobs._MODES
    # 生成用例 = 先出一版草稿用例（--until draft），停在草稿复核 + 人工回答前
    assert jobs._MODES["draft"]("2026-06-16") == [
        "--sprint", "2026-06-16", "--select", "--until", "draft"]
    assert jobs._MODE_LABEL["draft"] == "生成用例"
    # 重新生成 = 从 Jira 重抓、重做到草稿(--until draft --fresh)，回到「待确认」，不是 resume
    assert jobs._MODES["regen"]("2026-06-16") == [
        "--sprint", "2026-06-16", "--select", "--until", "draft", "--fresh"]
    assert jobs._MODE_LABEL["regen"] == "重新生成"
    assert jobs._MODES["regen"]("d") != jobs._MODES["resume"]("d")   # 与继续生成不同机制


def test_scope_to_keys_single_ticket():
    """单工单操作：把 --select 换成 --keys <EAR>，其余阶段标志不变；无 keys 则原样。"""
    from webapp.routers import jobs
    base = ["--sprint", "d", "--select", "--until", "questions", "--fresh"]
    scoped = jobs._scope_to_keys(base, ["EAR-1"])
    assert scoped == ["--sprint", "d", "--keys", "EAR-1", "--until", "questions", "--fresh"]
    assert "--select" not in scoped and base[2] == "--select"   # 未污染原 list
    # 多工单逗号连接
    assert jobs._scope_to_keys(["--sprint", "d", "--select"], ["EAR-1", "EAR-2"]) == [
        "--sprint", "d", "--keys", "EAR-1,EAR-2"]
    # 无 --select（rerun）则追加 --keys
    assert jobs._scope_to_keys(["--sprint", "d"], ["EAR-9"]) == ["--sprint", "d", "--keys", "EAR-9"]
    # 无 keys 原样返回
    assert jobs._scope_to_keys(["--sprint", "d", "--select"], []) == ["--sprint", "d", "--select"]


def test_jobmanager_exclusive_lock_blocks_concurrent():
    """删除用的 try_lock 与作业共用同一把产品串行锁 → 删除期间生成被挡、反之亦然。"""
    from webapp.jobs import JobManager
    m = JobManager()
    assert m.try_lock("wms", "delete:2026-06-16") is True
    assert m.is_busy("wms") == "delete:2026-06-16"
    assert m.try_lock("wms", "delete:other") is False      # 已锁 → 抢不到
    assert m._claim("wms", "job-x") is False               # 生成作业也被同一把锁挡住
    m.unlock("wms")
    assert m.is_busy("wms") is None
    assert m.try_lock("wms", "delete:again") is True        # 释放后可再抢
    m.unlock("wms")


# ----------------------------- 依据/复核 清洗 + 看板数字 -----------------------------

def test_evidence_strips_jql_and_search_process_keeps_real_sources():
    from webapp.services import humanize as hmz
    bt = chr(96)
    src = ("requirement.md §3 修改方案原句：“切换单位后按换算率修改”。"
           "linked-issues.md §1 EAR-252926 L1 修改方案摘录与本工单一致，无补充说明。"
           "_jira-search.md 已检索 " + bt + "project in (EAR) AND text ~ \"基本数量\" ORDER BY updated DESC" + bt
           + "、" + bt + "project in (EAR) AND summary ~ \"x\" ORDER BY updated DESC" + bt
           + "，未命中可确认本次范围的 L1 规则。")
    out = hmz.evidence(src)
    # 真实来源保留
    assert "切换单位后按换算率修改" in out
    assert "EAR-252926" in out and "关联工单" in out
    # 搜索过程/内部引用清除
    for bad in ("project in", "ORDER BY", "已检索", "未命中", "§", bt, "L1", ".md", "_jira-search"):
        assert bad not in out, (bad, out)
    assert "\n" in out                                      # 多来源分行


def test_evidence_drops_process_only_and_dangling_label():
    from webapp.services import humanize as hmz
    src = ('已查 关联工单 §EAR-252821、业务背景 、关联工单，未找到 /KB 明确确认“分拣员设定”属于参数可选项。'
           '业务背景 原句。')
    out = hmz.evidence(src)
    for bad in ("已查", "未找到", "§", "EAR-252821", "原句"):
        assert bad not in out, (bad, out)


def test_clean_review_keeps_structure_strips_refs():
    from webapp.services import humanize as hmz
    bt = chr(96)
    md = ("## 覆盖维度\n- 方案要求“X 字段”，用例已覆盖（rules.md §2.1）\n"
          "- 已检索 " + bt + "project in (EAR) ORDER BY updated DESC" + bt + "，未命中\n")
    out = hmz.clean_review(md)
    assert "## 覆盖维度" in out and "用例已覆盖" in out      # 标题/结论保留
    for bad in ("§", "project in", "已检索", "rules.md"):
        assert bad not in out, (bad, out)


def test_validator_platform_allowlist_includes_supplier_and_forward_warehouse():
    """校验器平台白名单须含 modules.md/style-notes 的合法平台（供应商平台/供应商app/前置仓app/TMS小程序）——
    之前漏了导致 EAR-250666 这类合法工单(方案范围明写供应商平台)被误判『校验有问题』。"""
    from webapp.services import scripts_loader
    vtd = scripts_loader.validate_test_design()
    for p in ("供应商平台", "供应商app", "前置仓app", "TMS小程序",
              "web", "仓配app", "采配app", "零售app", "POS", "TMS"):
        assert p in vtd.PLATFORM_PREFIXES, p


def test_badge_casevalidation_is_deliverable_only(tmp_path):
    """用例校验徽标只看 test-design.json(validate-test-design)，不被中间产物(check-ticket-artifacts)拖累：
    badge() 不再为内容契约跑子进程，content_rc 为中性值（0/-1），json_ok 才是用例状态真源。"""
    from webapp.services import tickets
    # 无 test-design：has_design False，content_rc=-1（无产物）
    b0 = tickets.badge(tmp_path)
    assert b0["has_design"] is False and b0["content_rc"] == -1
    # 结构非法的 test-design（空数组）：json_ok False → 用例状态『有问题』
    (tmp_path / "test-design.json").write_text("[]", encoding="utf-8")
    b1 = tickets.badge(tmp_path)
    assert b1["has_design"] is True and b1["json_ok"] is False   # 真用例问题仍被 json_ok 抓到


def test_evidence_quote_aware_linebreak_keeps_quotes_intact():
    """『依据』按真实来源分行：句号在引号内不得断行（原句保持完整）；L3 背景类来源整段删。"""
    from webapp.services import humanize as hmz
    src = ('修改方案原句：“查询门店后，选择门店，带入新增单据必要字段。”、“查询客户后，选择客户，带入新增单据必要字段。” '
           '背景信息提到“支持速记码-代码搜索”，关联讨论提到“批发客户信息，支持传入条件：客户代码”，但未形成明确规则。')
    out = hmz.evidence(src)
    lines = out.split("\n")
    assert len(lines) == 2, lines                          # 修改方案 + 关联讨论；背景信息(L3)被删
    # 引号内的句号没把原句拦腰断开：第一行同时含两段完整原句
    assert "查询门店后，选择门店，带入新增单据必要字段。" in lines[0]
    assert "查询客户后，选择客户，带入新增单据必要字段。" in lines[0]
    assert lines[0].startswith("修改方案")
    assert lines[1].startswith("关联讨论")
    assert "背景信息" not in out                            # L3 背景类不当依据


def test_evidence_drops_l3_background_sources():
    """L3『客户与背景/客户场景/期望目标』不是修改方案/业务规则，不当依据，整段删；L1 原句保留。"""
    from webapp.services import humanize as hmz
    src = ('修改方案原句：“按换算率修改”。本工单客户与背景提到“客户上百个、查询麻烦”。客户场景：“分拣称做销售”。')
    out = hmz.evidence(src)
    assert "按换算率修改" in out                            # L1 真来源保留
    for bad in ("客户与背景", "客户场景", "客户上百个", "分拣称做销售"):
        assert bad not in out, (bad, out)


def test_clean_review_strips_dev_locators_keeps_attribution():
    """复核证据：节点 id / 行号 / file:line / L 编号列表 等开发定位噪声清掉，真实溯源（工单+评论原句）保留。"""
    from webapp.services import humanize as hmz
    md = ('- 证据：test-design.json 节点 ...E112/E114/E124/E126 expect 均含“表头名称保持不变”；'
          'questions.md Q1 答案=“B”；linked-issues.md 第27行 EAR-246830 评论“对应的表格列标题和值会随其改变”；'
          'rules.md:3712“选采购单位时列表头变为采购单位”；test-design.json L70/L82/L144/L156；切面（DDDE109）')
    out = hmz.clean_review(md)
    for bad in ("E112", "E114", "第27行", "rules.md", "L70", "L144", "DDDE109",
                "test-design.json", "questions.md"):
        assert bad not in out, (bad, out)
    assert "EAR-246830" in out and "评论" in out
    assert "对应的表格列标题和值会随其改变" in out
    assert "测试用例" in out and "业务规则" in out


def test_board_has_pending_field(tmp_path, monkeypatch):
    from webapp.services import selection
    bd = selection.board("wms", "2026-06-16")
    if not bd["exists"]:
        import pytest
        pytest.skip("样本缺失")
    assert bd["pending"] == max(bd["run_count"] - bd["done_count"], 0)


def test_per_user_tickets_root_isolation():
    """每用户独立工单根：userdata/<用户名>/tickets；无会话回退 legacy；非法名不逃逸。"""
    from webapp import config
    assert config.tickets_root() == config.TICKETS_DIR          # 无会话 → legacy
    assert config.user_tickets_dir("alice").as_posix().endswith("userdata/alice/tickets")
    assert config.user_tickets_dir("bob") != config.user_tickets_dir("alice")
    assert "__invalid__" in config.user_tickets_dir("../etc").as_posix()   # 非法名隔离
    assert config.valid_username("a-1_B") and not config.valid_username("../x") and not config.valid_username("")
    config.set_user_root("alice")
    try:
        assert config.tickets_root() == config.user_tickets_dir("alice")   # 请求级跟随用户
    finally:
        config.set_user_root(None)
    assert config.tickets_root() == config.TICKETS_DIR


# ----------------------------- 草稿先行·只答一次（流程重排） -----------------------------

def _seed_board(tmp_path, monkeypatch, *, draft=False, design=False):
    """造一个有选单 + 1 单的最小看板树。draft/design 控制是否落 _draft-design.json / test-design.json。"""
    monkeypatch.setattr(config, "TICKETS_DIR", tmp_path)
    product, date, ear = "wms", "2026-06-16", "EAR-1"
    d = tmp_path / product / date / ear
    d.mkdir(parents=True)
    (d / "questions.md").write_text(
        "# 待确认问题清单 — EAR-1\n\n## Q1: 切换单位后是否按换算率重算？\n"
        "**问题**：方案未明确\n**来源**：本工单修改方案\n**可能场景**：A / B\n"
        "**影响范围**：金额预期\n**✅ 答案**：\n<!-- 待填 -->\n", encoding="utf-8")
    if draft:
        (d / "_draft-design.json").write_text("[]", encoding="utf-8")
    if design:
        (d / "test-design.json").write_text("[]", encoding="utf-8")
    sd = tmp_path / product / ".sprint-state"
    sd.mkdir(parents=True)
    (sd / f"_selection-{date}.json").write_text(json.dumps({
        "decisions": [{"key": ear, "decision": "run", "summary": "切换单位", "role": "main"}],
        "run_list": [ear], "candidate_count": 1,
    }, ensure_ascii=False), encoding="utf-8")
    return product, date, ear


def test_draft_design_invisible_to_board(tmp_path, monkeypatch):
    """草稿用例（_draft-design.json）不得被看板当成「已生成」——工单仍停在「待确认/可继续生成」，
    待确认数照常显示。这是流程重排的核心安全属性：草稿对状态机/徽标/digest 完全不可见。"""
    product, date, ear = _seed_board(tmp_path, monkeypatch, draft=True)
    bd = selection.board(product, date)
    row = bd["rows"][0]
    assert row["is_run"] and row["has_design"] is False          # 草稿不算已生成
    assert row["has_questions"] is True and row["status"] == "pending"
    assert row["q_pending"] == 1                                  # 待确认数照常显示
    assert bd["done_count"] == 0 and bd["awaiting_continue"] == 1  # 看板提示「继续生成」


def test_finalized_design_marks_done_on_board(tmp_path, monkeypatch):
    """出了正式 test-design.json 才算「已生成」（即便草稿也在，定稿后状态翻 done）。"""
    product, date, ear = _seed_board(tmp_path, monkeypatch, draft=True, design=True)
    bd = selection.board(product, date)
    row = bd["rows"][0]
    assert row["has_design"] is True and row["status"] == "done"
    assert bd["done_count"] == 1 and bd["awaiting_continue"] == 0


def test_review_questions_after_design_show_continue(tmp_path, monkeypatch):
    """复核后新增/更新 questions.md 时，即便已有正式用例，也必须能继续生成。"""
    product, date, ear = _seed_board(tmp_path, monkeypatch, design=True)
    d = tmp_path / product / date / ear
    td = d / "test-design.json"
    q = d / "questions.md"
    old = 1_000_000_000
    new = 2_000_000_000
    import os
    os.utime(td, ns=(old, old))
    os.utime(q, ns=(new, new))
    bd = selection.board(product, date)
    row = bd["rows"][0]
    assert row["has_design"] is True and row["needs_resume"] is True
    assert row["status"] == "pending"
    assert bd["done_count"] == 1 and bd["awaiting_continue"] == 1


def test_apply_writeback_appends_missing_without_report(tmp_path, monkeypatch):
    """草稿复核复用 resolve.apply_writeback 折入待确认：report=False 不覆盖 _resolve.md；
    needs_human 项作为占位 ## Q 追加、计入 pending（让人工只在这一份表里答）。"""
    from webapp.strong import resolve
    from webapp.services import questions as q_svc
    monkeypatch.setattr(config, "TICKETS_DIR", tmp_path)
    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    q = d / "questions.md"
    q.write_text("# 待确认问题清单 — EAR-1\n\n无\n", encoding="utf-8")
    item = {"question": "分拣员是否为参数可选项？", "status": "needs_human",
            "already_answered": False, "problem": "写确定预期需先知道", "source": "方案未明确",
            "possible_scenarios": ["是", "否"], "impact": "影响相关预期", "reason": "草稿复核：方案未明确"}
    counts = resolve.apply_writeback(q, [], [item], {}, report=False)
    assert counts["added_questions"] == 1
    assert not (d / "_resolve.md").exists()              # report=False：不写 resolve 报告
    parsed = q_svc.parse(q)
    assert parsed["form"] == "questions" and parsed["counts"]["pending"] >= 1
    assert "分拣员" in parsed["raw"]
    assert parsed["blocks"][0]["options"] == [
        {"key": "A", "text": "是"}, {"key": "B", "text": "否"}]


def test_apply_writeback_numbers_possible_scenarios_for_radio_options(tmp_path, monkeypatch):
    from webapp.strong import resolve
    from webapp.services import questions as q_svc
    monkeypatch.setattr(config, "TICKETS_DIR", tmp_path)
    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    q = d / "questions.md"
    q.write_text("# 待确认问题清单 — EAR-1\n\n无\n", encoding="utf-8")
    item = {"question": "A 与 B 同时配置时如何过滤？", "status": "needs_human",
            "already_answered": False, "problem": "写确定预期需先知道", "source": "需求方案原句：“支持 A、B 两类配置。”",
            "possible_scenarios": ["取交集", "分别过滤"], "impact": "影响查询结果预期", "reason": "需产品确认"}
    resolve.apply_writeback(q, [], [item], {}, report=False)
    parsed = q_svc.parse(q)
    assert parsed["blocks"][0]["options"] == [
        {"key": "A", "text": "取交集"}, {"key": "B", "text": "分别过滤"}]


def test_spot_check_fold_questions_requires_traceable_source(tmp_path, monkeypatch):
    from webapp.strong import spot_check
    from webapp.services import questions as q_svc
    monkeypatch.setattr(config, "TICKETS_DIR", tmp_path)
    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    q = d / "questions.md"
    q.write_text("# 待确认问题清单 — EAR-1\n\n无\n", encoding="utf-8")
    result = spot_check._fold_questions(d, [
        {"question": "无依据的问题？", "source": "复核发现（方案未明确）", "possible_scenarios": ["是", "否"]},
        {"question": "有依据的问题？", "source": "关联工单 EAR-2 评论：“按 A 处理。”", "possible_scenarios": ["按 A", "按 B"]},
    ], lambda msg: None)
    parsed = q_svc.parse(q)
    assert len(result["skipped"]) == 1 and len(result["accepted"]) == 1
    assert "无依据的问题" not in parsed["raw"]
    assert "有依据的问题" in parsed["raw"]
    assert parsed["blocks"][0]["options"] == [
        {"key": "A", "text": "按 A"}, {"key": "B", "text": "按 B"}]


def test_repair_guards_reject_shrink_and_bad_output(tmp_path, monkeypatch):
    """结构修复兜底的安全护栏：① 强模型未给出合法数组 → 放弃；② 测试点缩水(疑似删用例) → 放弃。
    两种情况都必须保留原用例字节不动、不写 .bak（仅过校验且不缩水才落盘——在别处的正向路径覆盖）。"""
    from webapp.strong import repair
    monkeypatch.setattr(config, "TICKETS_DIR", tmp_path)
    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    td = d / "test-design.json"
    td.write_text(json.dumps([{"text": "EAR-1", "children": [
        {"testPoint": {"id": "A" * 32}, "text": "tp1"},
        {"testPoint": {"id": "B" * 32}, "text": "tp2"}]}], ensure_ascii=False), encoding="utf-8")
    before = td.read_text(encoding="utf-8")
    # ① 非数组（如输出被截断 → None）→ 放弃，原文不动
    r0 = repair.apply_repair(td, None)
    assert r0["applied"] is False and td.read_text(encoding="utf-8") == before
    # ② 测试点缩水（2 → 1）→ 放弃，原文不动，不写 .bak
    r1 = repair.apply_repair(td, [{"text": "EAR-1", "children": [
        {"testPoint": {"id": "C" * 32}, "text": "only1"}]}])
    assert r1["applied"] is False and "测试点" in r1["notes"]
    assert td.read_text(encoding="utf-8") == before
    assert not td.with_name("test-design.json.bak").exists()


# ----------------------------- 作业 UX / 近期任务显示 -----------------------------


def test_strong_job_keeps_sprint_type_label_and_starts_thread(monkeypatch):
    from webapp.jobs import JobManager
    from webapp.strong import runner

    monkeypatch.setattr(runner, "availability_for", lambda endpoint: (True, "ok"))
    started = []

    class FakeThread:
        def __init__(self, target, daemon=False):
            self.target = target
            self.daemon = daemon

        def start(self):
            started.append((self.target, self.daemon))

    monkeypatch.setattr("webapp.jobs.threading.Thread", FakeThread)

    manager = JobManager()
    job, err = asyncio.run(manager.start_strong(
        "finalize", "wms", ["/tmp/EAR-1"], "复核 1 单", None, sprint="2026-06-16"))

    assert err is None
    data = job.public()
    assert data["sprint"] == "2026-06-16"
    assert data["type"] == "finalize"
    assert data["type_label"] == "复核"
    assert job.lines == ["任务已提交，正在连接复核模型…"]
    assert len(started) == 1 and started[0][1] is True


def test_spot_check_route_passes_sprint_to_strong_job(monkeypatch, tmp_path):
    from starlette.datastructures import URL
    from webapp.auth import User
    from webapp.routers import jobs
    from webapp.strong import runner

    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    (d / "test-design.json").write_text("[]", encoding="utf-8")
    calls = []

    class FakeRequest:
        url = URL("http://test/jobs/spot-check")
        headers = {}

    class FakeJob:
        lines = []

        @staticmethod
        def public():
            return {"id": "j1", "type": "finalize", "type_label": "复核", "product": "wms",
                    "sprint": "2026-06-16", "label": "复核 1 单", "user": "alice",
                    "status": "running", "started": "10:00:00", "finished": "", "rc": None}

    async def fake_start(kind, product, dirs, label, user, sprint=""):
        calls.append((kind, product, dirs, label, user.username, sprint))
        return FakeJob(), None

    monkeypatch.setattr(runner, "availability_for", lambda endpoint: (True, "ok"))
    monkeypatch.setattr(jobs, "user_anthropic_endpoint", lambda user: {})
    monkeypatch.setattr(jobs, "_run_dirs", lambda product, date, need: ([str(d)], ["EAR-1"]))
    monkeypatch.setattr(jobs.manager, "start_strong", fake_start)
    monkeypatch.setattr(jobs, "audit", lambda *args, **kwargs: None)

    user = User(username="alice")
    resp = asyncio.run(jobs.spot_check(FakeRequest(), "wms", "2026-06-16", "", user))

    assert resp.status_code == 200
    assert calls == [("finalize", "wms", [str(d)], "复核 1 单", "alice", "2026-06-16")]


def test_auto_post_logs_when_strong_job_cannot_start(monkeypatch, tmp_path):
    from webapp.auth import User
    from webapp.jobs import JobManager
    from webapp.services import tickets

    product, date, ear = _seed_board(tmp_path, monkeypatch, draft=True)
    d = tickets.find_ticket(product, ear)
    manager = JobManager()
    source_job = manager._new("generate", product, date, "生成用例", "alice")

    async def fake_start(*args, **kwargs):
        return None, "产品 wms 已有作业在运行。"

    monkeypatch.setattr(manager, "start_strong", fake_start)

    with pytest.raises(RuntimeError, match="已有作业"):
        asyncio.run(manager._auto_post(product, date, User(username="alice"), "review", [ear], source_job))

    assert d is not None
    assert any("自动强检查未启动" in line and "已有作业" in line for line in source_job.lines)


def test_spot_check_empty_findings_reports_checked_dimensions(monkeypatch, tmp_path):
    from webapp.strong import spot_check

    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    (d / "test-design.json").write_text("[]", encoding="utf-8")

    async def fake_query_json(*args, **kwargs):
        return {"findings": []}

    monkeypatch.setattr("webapp.strong.runner.query_json", fake_query_json)
    logs = []
    result = asyncio.run(spot_check._run_one(str(d), "wms", logs.append))

    md = (d / "_spot-check.md").read_text(encoding="utf-8")
    assert result["checked"] == len(spot_check.schemas.SPOT_CHECK_DIMS)
    assert "已检查 5/5 个复核维度" in md
    assert "已复核 0 条" not in md
    assert "未发现需修正的问题" in md
    assert any("已检查 5/5 个维度" in line for line in logs)


def test_spotcheck_html_warns_for_legacy_zero_result(tmp_path):
    from webapp.services import digest

    d = tmp_path / "EAR-1"
    d.mkdir()
    (d / "_spot-check.md").write_text(
        "# 复核结果 — EAR-1\n\n> 已复核 0 条 · 确认 0 条建议\n\n✅ 未发现需修正的问题。\n",
        encoding="utf-8")

    html = digest.spotcheck_html(d)
    assert "旧版复核记录" in html
    assert "不能证明本次复核已成功完成" in html


def test_spot_check_all_dimensions_fail_does_not_write_fake_success(monkeypatch, tmp_path):
    from webapp.strong import runner, spot_check

    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    (d / "test-design.json").write_text("[]", encoding="utf-8")

    async def fake_query_json(*args, **kwargs):
        raise runner.StrongModelOutputError("bad json")

    monkeypatch.setattr("webapp.strong.runner.query_json", fake_query_json)

    with pytest.raises(RuntimeError, match="全部复核维度失败"):
        asyncio.run(spot_check._run_one(str(d), "wms", lambda msg: None))
    assert not (d / "_spot-check.md").exists()


def test_spot_check_run_writes_failure_report_and_raises(monkeypatch, tmp_path):
    from webapp.strong import runner, spot_check

    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    (d / "test-design.json").write_text("[]", encoding="utf-8")

    async def fake_query_json(*args, **kwargs):
        raise runner.StrongModelOutputError("bad json")

    monkeypatch.setattr("webapp.strong.runner.query_json", fake_query_json)

    with pytest.raises(RuntimeError, match="1 单复核失败"):
        asyncio.run(spot_check.run([str(d)], "wms", lambda msg: None))
    md = (d / "_spot-check.md").read_text(encoding="utf-8")
    assert "本次复核失败" in md
    assert "bad json" in md
    assert "已复核 0 条" not in md


def test_revise_postcheck_retries_leftover_text_issue(monkeypatch, tmp_path):
    from webapp.strong import revise

    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    (d / "_spot-check.md").write_text(
        "# 复核结果 — EAR-1\n\n> 已检查 5/5 个复核维度 · 确认 1 条建议\n\n"
        "- 问题：门店/客户选择预期混入未确认的弹窗关闭。\n",
        encoding="utf-8")
    (d / "requirement.md").write_text("## 3. 修改方案\n查询门店后选择门店，带入必要字段；查询客户后选择客户，带入必要字段。\n", encoding="utf-8")
    (d / "questions.md").write_text("# 待确认问题清单 — EAR-1\n\n无\n", encoding="utf-8")
    td = d / "test-design.json"
    td.write_text(json.dumps([
        {"id": "A" * 32, "text": "EAR-1", "children": [
            {"id": "B" * 32, "text": "弹窗关闭，所选门店信息正确带入", "expect": "Y"},
            {"id": "C" * 32, "text": "弹窗关闭，所选客户信息正确带入", "expect": "Y"},
        ]}
    ], ensure_ascii=False), encoding="utf-8")

    class FakeBatch:
        @staticmethod
        def run_validator(*args):
            return 0, "ok"

    calls = {"n": 0}

    async def fake_query_json(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"edits": [{"node_id": "B" * 32, "new_text": "所选门店信息正确带入", "reason": "需求方案原句支撑"}],
                    "questions": [], "unfixable": []}
        if calls["n"] == 2:
            return {"resolved": False, "leftover": [{
                "finding": "客户侧仍残留未确认的弹窗关闭",
                "fix_type": "text", "node_id": "C" * 32,
                "current_text": "弹窗关闭，所选客户信息正确带入",
                "suggested_new_text": "所选客户信息正确带入",
                "source": "需求方案原句：“查询客户后选择客户，带入必要字段。”",
                "why": "同源同类节点仍有未确认 UI 生命周期预期",
            }], "reasoning": "仍有残留"}
        return {"resolved": True, "leftover": [], "reasoning": "残留已补修"}

    monkeypatch.setattr(revise.scripts_loader, "batch_generate", lambda: FakeBatch())
    monkeypatch.setattr("webapp.strong.runner.query_json", fake_query_json)

    res = asyncio.run(revise.fix_one(str(d), lambda msg: None))
    data = json.loads(td.read_text(encoding="utf-8"))
    text = json.dumps(data, ensure_ascii=False)
    md = (d / "_revise.md").read_text(encoding="utf-8")
    assert res["applied"] == 2
    assert "弹窗关闭" not in text
    assert "修后验收通过" in md
    assert "修后验收补漏" in md


def test_review_status_marks_unfixable_and_revise_signature(monkeypatch, tmp_path):
    import os
    from webapp.services import digest, tickets

    monkeypatch.setattr(config, "TICKETS_DIR", tmp_path)
    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    (d / "test-design.json").write_text("[]", encoding="utf-8")
    os.utime(d / "test-design.json", ns=(1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(tickets, "json_structure_issues", lambda directory: [])
    monkeypatch.setattr(tickets, "_stats", lambda directory: {"testpoints": 1, "todo": 0})
    tickets.invalidate_badge(d)
    b0 = tickets.badge(d)
    assert b0["review_needs_action"] is False

    (d / "_revise.md").write_text(
        "# 复核修复记录 — EAR-1\n\n## 需重新生成 / 人工补（仅改文本无法修复）\n"
        "- 切换回代码搜索漏覆盖 —— 需要新增步骤。\n", encoding="utf-8")
    os.utime(d / "_revise.md", ns=(2_000_000_000, 2_000_000_000))
    b1 = tickets.badge(d)
    built = digest.build(d)
    assert b1["review_needs_action"] is True
    assert b1["review_unfixable"] is True
    assert "重新生成或人工补" in b1["review_summary"]
    assert built["json_ready"] is True and built["ready"] is False


def test_draft_review_run_writes_failure_report_and_raises(monkeypatch, tmp_path):
    from webapp.strong import draft_review, runner

    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    (d / "_draft-design.json").write_text("[]", encoding="utf-8")
    (d / "questions.md").write_text("# 待确认问题清单 — EAR-1\n\n无\n", encoding="utf-8")

    async def fake_query_json(*args, **kwargs):
        raise runner.StrongModelOutputError("bad json")

    monkeypatch.setattr("webapp.strong.runner.query_json", fake_query_json)

    with pytest.raises(RuntimeError, match="1 单草稿复核失败"):
        asyncio.run(draft_review.run([str(d)], "wms", lambda msg: None))
    md = (d / "_draft-review.md").read_text(encoding="utf-8")
    assert "本次草稿复核失败" in md
    assert "bad json" in md
    assert "未发现需要人工额外确认" not in md


def test_draft_review_blocks_untraceable_human_questions(monkeypatch, tmp_path):
    from webapp.strong import draft_review

    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    (d / "_draft-design.json").write_text("[]", encoding="utf-8")
    (d / "questions.md").write_text("# 待确认问题清单 — EAR-1\n\n无\n", encoding="utf-8")
    calls = {"n": 0}

    async def fake_query_json(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= len(draft_review.schemas.SPOT_CHECK_DIMS):
            return {"findings": [{"dimension": "诚信", "severity": "high", "test_point": "TP",
                                  "problem": "草稿写成确定规则", "evidence": "草稿 expect", "suggested_fix": "转人工确认"}]}
        return {"needs_human": True, "question": "本工单规则如何处理？", "problem": "写确定预期需先知道",
                "possible_scenarios": ["按 A", "按 B"], "impact": "影响预期",
                "source": "草稿复核发现", "reasoning": "缺少真实来源"}

    monkeypatch.setattr("webapp.strong.runner.query_json", fake_query_json)

    with pytest.raises(RuntimeError, match="缺少可追溯依据"):
        asyncio.run(draft_review.run([str(d)], "wms", lambda msg: None))
    md = (d / "_draft-review.md").read_text(encoding="utf-8")
    assert "未写入的待确认候选" in md
    assert "草稿复核发现" in md
    assert "本工单规则如何处理" in md


def test_resolve_run_writes_failure_report_and_raises(monkeypatch, tmp_path):
    from webapp.strong import resolve, runner

    d = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    d.mkdir(parents=True)
    (d / "questions.md").write_text("# 待确认问题清单 — EAR-1\n\n无\n", encoding="utf-8")

    async def fake_query_json(*args, **kwargs):
        raise runner.StrongModelOutputError("bad json")

    monkeypatch.setattr("webapp.strong.runner.query_json", fake_query_json)

    with pytest.raises(RuntimeError, match="1 单预答失败"):
        asyncio.run(resolve.run([str(d)], "wms", lambda msg: None))
    md = (d / "_resolve.md").read_text(encoding="utf-8")
    assert "本次证据消解失败" in md
    assert "bad json" in md


def test_openai_responses_missing_falls_back_to_chat():
    from types import SimpleNamespace
    from webapp.strong import runner

    calls = []

    class FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"findings": []}'),
                )]
            )

    class FakeClient:
        chat = SimpleNamespace(completions=FakeCompletions())

    result = asyncio.run(runner._query_openai(
        "prompt", system=None,
        ep={"base_url": "http://example.invalid", "api_key": "x", "model": "m"},
        client_factory=lambda **kwargs: FakeClient()))

    assert result == '{"findings": []}'
    assert calls and calls[0]["model"] == "m"


def test_query_json_raises_on_unparseable_model_output(monkeypatch):
    from webapp.strong import runner

    async def fake_query_text(*args, **kwargs):
        return "not json"

    monkeypatch.setattr(runner, "_query_text", fake_query_text)

    with pytest.raises(runner.StrongModelOutputError):
        asyncio.run(runner.query_json("prompt", shape_hint="{}"))


def test_run_sprint_exit_ignores_legacy_artifact_fail_when_json_passes(monkeypatch, tmp_path):
    from webapp.services import scripts_loader

    run_sprint = scripts_loader.run_sprint()
    ticket_dir = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "test-design.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr(run_sprint, "TICKETS_ROOT", tmp_path)
    monkeypatch.setattr(run_sprint, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_sprint, "find_dir", lambda product, key: ticket_dir)
    monkeypatch.setattr(run_sprint, "run_one", lambda *args, **kwargs: {
        "key": "EAR-1", "ok": True, "err": None, "dir": str(ticket_dir), "log": "", "sec": 0,
    })
    monkeypatch.setattr(run_sprint, "ticket_stats", lambda d: (0, 1, {"testpoints": 1, "todo": 0}))
    monkeypatch.setattr(sys, "argv", ["run_sprint.py", "--product", "wms", "--keys", "EAR-1", "--sprint", "2026-06-16"])

    assert run_sprint.main() == 0
    summary = (tmp_path / "wms" / ".sprint-state" / "_sprint-summary-2026-06-16.md").read_text(encoding="utf-8")
    assert "| EAR-1 | PASS | FAIL(1) |" in summary


def test_run_sprint_exit_fails_when_json_fails(monkeypatch, tmp_path):
    from webapp.services import scripts_loader

    run_sprint = scripts_loader.run_sprint()
    ticket_dir = tmp_path / "wms" / "2026-06-16" / "EAR-1"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / "test-design.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr(run_sprint, "TICKETS_ROOT", tmp_path)
    monkeypatch.setattr(run_sprint, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_sprint, "find_dir", lambda product, key: ticket_dir)
    monkeypatch.setattr(run_sprint, "run_one", lambda *args, **kwargs: {
        "key": "EAR-1", "ok": True, "err": None, "dir": str(ticket_dir), "log": "", "sec": 0,
    })
    monkeypatch.setattr(run_sprint, "ticket_stats", lambda d: (1, 0, {"testpoints": 0, "todo": 0}))
    monkeypatch.setattr(sys, "argv", ["run_sprint.py", "--product", "wms", "--keys", "EAR-1", "--sprint", "2026-06-16"])

    assert run_sprint.main() == 1
