"""作业管理器：弱链子进程驱动 + 强模型异步审计 + 单产品串行锁 + 进度行流。

- 弱链批量：后台线程跑 `python scripts/run_sprint.py ...`（argv 列表，绝不 shell=True），
  逐行读 stdout 进 job.lines；前端 SSE 轮询 job.lines（跨平台稳健，避开 Windows
  asyncio 子进程事件循环坑）。注入触发用户的 Jira PAT 到子进程 env。
- 强模型：spot-check / resolve 走 strong/ 的 asyncio 端口，on_log 回调写 job.lines。
- 单产品串行锁：同一 product 同时只允许一个生成/审计作业（防两人争抢同一批文件）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import config
from .auth import User
from .deps import redact_secret, subprocess_env, user_anthropic_endpoint
from core.store.repositories import OptionalJobRunMirror


JOB_TYPE_LABELS = {
    "generate": "生成",
    "spot-check": "复核",
    "finalize": "复核",
    "review": "草稿复核",
    "resolve": "预答",
    "kb-extract": "知识回填",
}


@dataclass
class Job:
    id: str
    type: str          # generate | spot-check | resolve | review | kb-extract
    product: str
    sprint: str
    label: str
    user: str
    status: str = "running"   # running | done | failed
    started: str = ""
    finished: str = ""
    lines: list[str] = field(default_factory=list)
    rc: Optional[int] = None
    argv_display: str = ""     # 供「复制命令」兜底

    def public(self) -> dict:
        return {
            "id": self.id, "type": self.type, "type_label": JOB_TYPE_LABELS.get(self.type, self.type),
            "product": self.product, "sprint": self.sprint,
            "label": self.label, "user": self.user, "status": self.status,
            "started": self.started, "finished": self.finished, "rc": self.rc,
            "line_count": len(self.lines), "argv_display": self.argv_display,
        }


class JobManager:
    def __init__(self, mirror: OptionalJobRunMirror | None = None) -> None:
        self.jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._active: dict[str, str] = {}   # lock_key -> job_id
        self._guard = threading.Lock()
        self._mirror = mirror or OptionalJobRunMirror(config.platform_engine())

    def _lock_key(self, product: str, user: str | None = None) -> str:
        return f"{user or '-'}:{product}"

    def _db_session(self):
        engine = config.platform_engine()
        if engine is None:
            return None
        from core.store.db import session_scope
        return session_scope(engine)

    def _append_log(self, job: Job, line: str, *, mirror: bool = True) -> None:
        clean = redact_secret(line)
        job.lines.append(clean)
        if mirror:
            self._mirror.log(job.id, clean)

    def _job_from_run(self, run, lines: list[str] | None = None) -> Job:
        started = run.started_at or run.created_at
        finished = run.finished_at
        return Job(
            id=run.legacy_job_id,
            type=run.type,
            product=run.product_key,
            sprint=run.sprint or "",
            label=run.label or "",
            user=run.owner_username or "",
            status="running" if run.status in {"queued", "running"} else run.status,
            started=started.strftime("%H:%M:%S") if started else "",
            finished=finished.strftime("%H:%M:%S") if finished else "",
            lines=list(lines or []),
            rc=run.rc,
            argv_display=run.argv_display or "",
        )

    def _db_get_job(self, job_id: str, owner_username: str | None = None) -> Job | None:
        ctx = self._db_session()
        if ctx is None:
            return None
        try:
            from core.store.repositories import JobRunRepository
            with ctx as session:
                repo = JobRunRepository(session)
                run = repo.get_run(job_id, owner_username=owner_username)
                if run is None:
                    return None
                return self._job_from_run(run, repo.logs(job_id, owner_username=owner_username))
        except Exception:
            return None

    def _db_recent(self, n: int = 8, product: Optional[str] = None, owner: str | None = None) -> list[Job] | None:
        ctx = self._db_session()
        if ctx is None:
            return None
        try:
            from core.store.repositories import JobRunRepository
            with ctx as session:
                repo = JobRunRepository(session)
                return [self._job_from_run(run) for run in repo.list_runs(limit=n, product_key=product, owner_username=owner)]
        except Exception:
            return None

    # ---- 串行锁 ----
    def is_busy(self, product: str, user: str | None = None) -> Optional[str]:
        if config.platform_engine() is not None:
            recent = self._db_recent(50, product, user)
            if recent is not None:
                running = next((job.id for job in recent if job.status == "running"), None)
                if running:
                    return running
        with self._guard:
            return self._active.get(self._lock_key(product, user))

    def _claim(self, product: str, job_id: str, user: str | None = None) -> bool:
        key = self._lock_key(product, user)
        with self._guard:
            if key in self._active:
                return False
            self._active[key] = job_id
            return True

    def _release(self, product: str, user: str | None = None) -> None:
        with self._guard:
            self._active.pop(self._lock_key(product, user), None)

    def try_lock(self, product: str, holder: str, user: str | None = None) -> bool:
        """为非作业的独占操作抢占 user:product 串行锁：成功 True、已忙 False。"""
        return self._claim(product, holder, user)

    def unlock(self, product: str, user: str | None = None) -> None:
        self._release(product, user)

    # ---- 作业表 ----
    def _new(self, type_: str, product: str, sprint: str, label: str, user: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], type=type_, product=product, sprint=sprint,
                  label=label, user=user, started=datetime.now().strftime("%H:%M:%S"))
        with self._guard:
            self.jobs[job.id] = job
            self._order.insert(0, job.id)
            del self._order[50:]  # 只保留最近 50 条
        return job

    def get(self, job_id: str, owner_username: str | None = None) -> Optional[Job]:
        db_job = self._db_get_job(job_id, owner_username=owner_username)
        if db_job is not None:
            return db_job
        job = self.jobs.get(job_id)
        if job is not None and owner_username is not None and job.user != owner_username:
            return None
        return job

    def recent(self, n: int = 8, product: Optional[str] = None, user: str | None = None) -> list[Job]:
        db_jobs = self._db_recent(n, product, user)
        if db_jobs is not None:
            return db_jobs
        out = [self.jobs[i] for i in self._order if i in self.jobs]
        if product:
            out = [j for j in out if j.product == product]
        if user:
            out = [j for j in out if j.user == user]
        return out[:n]

    def log_entries_after(self, job_id: str, *, after_seq: int = 0, owner_username: str | None = None) -> list[dict]:
        ctx = self._db_session()
        if ctx is None:
            return []
        try:
            from core.store.repositories import JobRunRepository
            with ctx as session:
                return JobRunRepository(session).log_entries_after(job_id, after_seq=after_seq, owner_username=owner_username)
        except Exception:
            return []

    def _finish(self, job: Job, status: str, rc: Optional[int]) -> None:
        job.status = status
        job.rc = rc
        job.finished = datetime.now().strftime("%H:%M:%S")
        self._release(job.product, job.user)
        self._mirror.finish(job.id, status=status, rc=rc)

    # ---- 弱链子进程 ----
    def start_generate(self, product: str, sprint: str, extra_args: list[str],
                       label: str, user: Optional[User],
                       post_strong: Optional[str] = None,
                       post_keys: Optional[list[str]] = None) -> tuple[Optional[Job], Optional[str]]:
        owner = user.username if user else "-"
        busy = self.is_busy(product, owner)
        if busy:
            return None, f"产品 {product} 已有作业在运行（{busy}），请等其完成。"
        job = self._new("generate", product, sprint, label, owner)
        if not self._claim(product, job.id, owner):
            return None, f"产品 {product} 已有作业在运行。"

        argv = [sys.executable, str(config.SCRIPTS_DIR / "run_sprint.py"),
                "--product", product]
        if "--concurrency" not in extra_args:
            argv += ["--concurrency", str(config.DEFAULT_CONCURRENCY)]
        argv += extra_args
        # 展示用命令（脱可执行路径噪声）
        job.argv_display = "python scripts/run_sprint.py " + " ".join(
            argv[argv.index("--product"):])
        env = subprocess_env(user)
        env["QA_PRODUCT"] = product
        start_status = "queued" if config.USE_WORKER else "running"
        self._mirror.start(
            legacy_job_id=job.id, type_="generate", product_key=product, sprint=sprint,
            label=label, owner_username=owner, lock_key=self._lock_key(product, owner),
            argv_display=job.argv_display, queue_name="generation", status=start_status,
            metadata={"post_strong": post_strong or "", "post_keys": post_keys or [], "args": extra_args},
        )
        if config.USE_WORKER:
            try:
                from worker.tasks import defer_generation
                defer_generation(job_id=job.id, username=owner, product=product, sprint=sprint, args=extra_args)
                self._append_log(job, "任务已提交到 worker 队列，等待执行…")
                return job, None
            except Exception as e:  # noqa: BLE001
                self._append_log(job, f"[入队失败] {type(e).__name__}: {e}")
                self._finish(job, "failed", -1)
                return job, None
        # 生成成功后自动接强模型阶段：post_strong="review"（草稿复核+预答；生成用例/重新生成后，
        # 把需人工的点汇总进 questions.md，让人工只答一次）或 "spot-check"（语义复核+文字自动修复；
        # 继续生成定稿后）。捕获事件循环，供子进程线程完成时跨线程调度。
        loop = None
        if post_strong:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        t = threading.Thread(target=self._run_subprocess,
                             args=(job, argv, env, user, post_strong, post_keys, loop), daemon=True)
        t.start()
        return job, None

    def _run_subprocess(self, job: Job, argv: list[str], env: dict,
                        user: Optional[User] = None, post_strong: Optional[str] = None,
                        post_keys: Optional[list[str]] = None, loop=None) -> None:
        try:
            proc = subprocess.Popen(
                argv, cwd=str(config.REPO_ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except Exception as e:  # noqa: BLE001
            self._append_log(job, f"[启动失败] {type(e).__name__}: {e}")
            self._finish(job, "failed", -1)
            return
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                self._append_log(job, raw.rstrip("\n"))
            rc = proc.wait()
        except Exception as e:  # noqa: BLE001
            self._append_log(job, f"[读取输出异常] {type(e).__name__}: {e}")
            rc = proc.poll() if proc.poll() is not None else -1
        self._finish(job, "done" if rc == 0 else "failed", rc)
        # 生成成功 → 自动接强模型阶段（review / finalize）。跨线程调度到事件循环；失败必须可见，
        # 否则用户会把「自动强检查没启动/启动失败」误认为「强检查通过但无新增待确认」。
        if rc == 0 and user is not None and post_strong:
            if loop is None:
                self._append_log(job, f"[自动强检查未启动] 无可用事件循环，请手动执行 {post_strong}。")
            else:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._auto_post(job.product, job.sprint, user, post_strong, post_keys, job), loop)
                    future.add_done_callback(lambda f: self._log_auto_post_result(job, post_strong, f))
                except Exception as e:  # noqa: BLE001
                    self._append_log(job, f"[自动强检查启动失败] {type(e).__name__}: {e}")

    def _log_auto_post_result(self, job: Job, post_strong: str, future) -> None:
        try:
            future.result()
        except Exception as e:  # noqa: BLE001
            self._append_log(job, f"[自动强检查调度失败] {post_strong}: {type(e).__name__}: {e}")

    async def _auto_post(self, product: str, sprint: str, user: Optional[User],
                         post_strong: str, post_keys: Optional[list[str]] = None,
                         source_job: Optional[Job] = None) -> None:
        """生成成功后自动接强模型阶段：
        - "review"：找仍停在待确认(有 questions、无正式 test-design.json)的单 → 预答 + 草稿复核，
          把需人工的点汇总进 questions.md（让人工只答一次；草稿不存在则退化为只预答）。
        - "finalize"：找已定稿(有 test-design.json)的单 → 结构修复 + 语义复核 + 文字自动修复。
        强模型未配 / 产品忙 / 无目标单必须写入生成日志，不能静默跳过。"""
        from .services import selection, tickets
        bd = selection.board(product, sprint)
        keep = set(post_keys or [])
        dirs = []
        for row in bd.get("rows", []):
            if not row.get("is_run"):
                continue
            key = row.get("key")
            if keep and key not in keep:
                continue
            td = tickets.find_ticket(product, key)
            if not td:
                continue
            has_design = (td / "test-design.json").exists()
            if post_strong == "review":
                if (td / "questions.md").exists() and not has_design:
                    dirs.append(str(td))
            elif post_strong in ("spot-check", "finalize"):
                if has_design:
                    dirs.append(str(td))
        if not dirs:
            if source_job:
                self._append_log(source_job, f"自动强检查未启动：没有找到 {post_strong} 的目标工单。")
            return
        label = (f"自动复核草稿 {len(dirs)} 单" if post_strong == "review"
                 else f"自动复核 {len(dirs)} 单")
        strong_job, err = await self.start_strong(post_strong, product, dirs, label, user, sprint=sprint)
        if err:
            if source_job:
                self._append_log(source_job, f"自动强检查未启动：{err}")
            raise RuntimeError(err)
        if source_job and strong_job:
            self._append_log(source_job, f"已启动自动强检查：{strong_job.id}（{label}）")

    # ---- 强模型异步审计 ----
    async def start_strong(self, kind: str, product: str, ticket_dirs: list[str],
                           label: str, user: Optional[User],
                           sprint: str = "") -> tuple[Optional[Job], Optional[str]]:
        from .strong import runner
        ep = user_anthropic_endpoint(user)
        ok, reason = runner.availability_for(ep)
        if not ok:
            return None, reason
        owner = user.username if user else "-"
        busy = self.is_busy(product, owner)
        if busy:
            return None, f"产品 {product} 已有作业在运行（{busy}）。"
        job = self._new(kind, product, sprint, label, owner)
        if not self._claim(product, job.id, owner):
            return None, f"产品 {product} 已有作业在运行。"
        self._mirror.start(
            legacy_job_id=job.id, type_=kind, product_key=product, sprint=sprint,
            label=label, owner_username=owner, lock_key=self._lock_key(product, owner),
            argv_display="", queue_name="review", metadata={"ticket_dirs": len(ticket_dirs)},
        )
        self._append_log(job, "任务已提交，正在连接复核模型…")

        async def _go():
            runner.set_endpoint(ep)  # 本 task 内用该用户的强模型端点（contextvar 隔离）
            try:
                def on_log(msg: str) -> None:
                    self._append_log(job, msg)
                if kind == "spot-check":
                    from .strong import spot_check
                    self._append_log(job, f"开始复核 {len(ticket_dirs)} 单…")
                    await spot_check.run(ticket_dirs, product, on_log)
                    self._append_log(job, "复核完成：已写入新版复核结果。")
                elif kind == "finalize":
                    # 继续生成定稿后的自动终检：先把结构坏的(json_ok==False)强模型兜底修复，
                    # 再对全部做语义复核 + 文字自动修复。
                    from .strong import repair, spot_check
                    self._append_log(job, f"开始定稿终检 {len(ticket_dirs)} 单（结构修复 + 语义复核）…")
                    await repair.run(ticket_dirs, product, on_log)
                    await spot_check.run(ticket_dirs, product, on_log)
                    self._append_log(job, "定稿终检完成：已写入新版复核结果。")
                elif kind == "resolve":
                    from .strong import resolve
                    self._append_log(job, f"开始预答 {len(ticket_dirs)} 单…")
                    await resolve.run(ticket_dirs, product, on_log)
                elif kind == "review":
                    # 生成用例后的自动「审」：先证据消解(分析级)，再对草稿用例复核(草稿级)，
                    # 两步都把需人工的点折进 questions.md，让人工只答一次。
                    from .strong import draft_review, resolve
                    self._append_log(job, f"开始预答 + 草稿复核 {len(ticket_dirs)} 单…")
                    await resolve.run(ticket_dirs, product, on_log)
                    await draft_review.run(ticket_dirs, product, on_log)
                elif kind == "kb-extract":
                    from .strong import kb_extract
                    self._append_log(job, f"开始提炼可入库规则 {len(ticket_dirs)} 单…")
                    await kb_extract.run(ticket_dirs, product, on_log)
                else:
                    raise ValueError(f"未知强模型作业：{kind}")
                self._finish(job, "done", 0)
            except Exception as e:  # noqa: BLE001
                self._append_log(job, f"[任务失败] {type(e).__name__}: {e}")
                self._finish(job, "failed", -1)

        def _run_thread() -> None:
            try:
                asyncio.run(_go())
            except Exception as e:  # noqa: BLE001
                self._append_log(job, f"[任务失败] {type(e).__name__}: {e}")
                self._finish(job, "failed", -1)

        threading.Thread(target=_run_thread, daemon=True).start()
        return job, None


manager = JobManager()
