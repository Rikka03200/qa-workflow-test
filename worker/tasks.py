"""Procrastinate task declarations for platform execution."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from core.store.db import session_scope
from core.store.repositories import JobRunRepository
from webapp import auth, config
from webapp.deps import subprocess_env, user_anthropic_endpoint
from webapp.services import artifacts

from .app import app


def lock_key(username: str, product: str) -> str:
    return f"{username or '-'}:{product}"


def _repo_call(fn):
    engine = config.platform_engine()
    if engine is None:
        return None
    with session_scope(engine) as session:
        return fn(JobRunRepository(session))


def _mark_running(job_id: str) -> None:
    def _run(repo: JobRunRepository):
        run = repo.get_run(job_id)
        if run is not None:
            run.status = "running"
            run.started_at = run.started_at or datetime.utcnow()
    _repo_call(_run)


def _log(job_id: str, line: str) -> None:
    _repo_call(lambda repo: repo.append_log(job_id, line))


def _finish(job_id: str, status: str, rc: int | None) -> None:
    _repo_call(lambda repo: repo.finish_run(job_id, status=status, rc=rc))


def _prepare_ticket_root(job_id: str, username: str, product: str, sprint: str):
    if config.USE_DB_ARTIFACTS:
        root = artifacts.work_root(job_id)
        tickets_root, count = artifacts.materialize_sprint(owner_username=username, product=product, sprint=sprint, root=root)
        _log(job_id, f"DB artifact 模式：已物化 {count} 个工单到 {tickets_root}。")
        config.set_tickets_root(tickets_root)
        return root, tickets_root
    tickets_root = config.user_tickets_dir(username) if username and username != "-" else config.TICKETS_DIR
    config.set_tickets_root(tickets_root)
    return None, tickets_root


def _mirror_after_run(root, username: str, job_id: str, product: str = "", sprint: str = "") -> None:
    if config.USE_DB_ARTIFACTS and root is not None:
        count = artifacts.mirror_materialized_tree(root, owner_username=username)
        _log(job_id, f"DB artifact 模式：已回灌 {count} 个产物。")
        if product and sprint:
            exported = artifacts.export_user_cache(owner_username=username, product=product, sprint=sprint)
            _log(job_id, f"DB artifact 模式：已导出 {exported} 个工单到用户兼容缓存。")


def _work_root_for_run(job_id: str):
    def _get(repo: JobRunRepository):
        run = repo.get_run(job_id)
        value = (run.metadata_json or {}).get("work_root") if run is not None else ""
        return Path(value) if value else None
    return _repo_call(_get)


def _run_dirs(product: str, sprint: str, kind: str, keep: list[str] | None = None, root=None) -> list[str]:
    from webapp.services import selection, tickets

    wanted = set(keep or [])
    board = selection.board(product, sprint)
    dirs = []
    base = root / "tickets" if root is not None else None
    for row in board.get("rows", []):
        if not row.get("is_run"):
            continue
        key = row.get("key")
        if wanted and key not in wanted:
            continue
        ticket_dir = (base / product / sprint / key) if base is not None else tickets.find_ticket(product, key)
        if not ticket_dir or not ticket_dir.exists():
            continue
        has_design = (ticket_dir / "test-design.json").exists()
        if kind == "review" and (ticket_dir / "questions.md").exists() and not has_design:
            dirs.append(str(ticket_dir))
        elif kind in {"spot-check", "finalize", "kb-extract"} and has_design:
            dirs.append(str(ticket_dir))
        elif kind == "resolve" and (ticket_dir / "questions.md").exists():
            dirs.append(str(ticket_dir))
    return dirs


def _queue_post_review(source_job_id: str, username: str, product: str, sprint: str, root=None) -> None:
    def _make(repo: JobRunRepository):
        source = repo.get_run(source_job_id)
        if source is None:
            return None
        post_kind = (source.metadata_json or {}).get("post_strong") or ""
        if not post_kind:
            return None
        post_keys = (source.metadata_json or {}).get("post_keys") or []
        dirs = _run_dirs(product, sprint, post_kind, post_keys, root=root)
        if not dirs:
            repo.append_log(source_job_id, f"自动强检查未启动：没有找到 {post_kind} 的目标工单。")
            return None
        job_id = uuid.uuid4().hex[:12]
        label = (f"自动复核草稿 {len(dirs)} 单" if post_kind == "review" else f"自动复核 {len(dirs)} 单")
        repo.start_run(
            legacy_job_id=job_id,
            type_=post_kind,
            product_key=product,
            sprint=sprint,
            label=label,
            owner_username=username,
            lock_key=lock_key(username, product),
            queue_name="review",
            status="queued",
            metadata={"ticket_dirs": len(dirs), "source_job_id": source_job_id, "work_root": str(root) if root else ""},
        )
        repo.append_log(source_job_id, f"已启动自动强检查：{job_id}（{label}）")
        return job_id, post_kind, dirs

    payload = _repo_call(_make)
    if payload:
        job_id, post_kind, dirs = payload
        defer_review(job_id=job_id, username=username, product=product, sprint=sprint, kind=post_kind, ticket_dirs=dirs)


@app.task(name="qa.generation.run_sprint", queue="generation")
def run_sprint(job_id: str, username: str, product: str, sprint: str, args: list[str] | None = None) -> dict:
    _mark_running(job_id)
    _log(job_id, "worker 已领取生成任务。")
    user = auth.store.get(username) if username and username != "-" else None
    work_root, tickets_root = _prepare_ticket_root(job_id, username, product, sprint)
    extra_args = list(args or [])
    argv = [sys.executable, str(config.SCRIPTS_DIR / "run_sprint.py"), "--product", product]
    if "--concurrency" not in extra_args:
        argv += ["--concurrency", str(config.DEFAULT_CONCURRENCY)]
    argv += extra_args
    env = subprocess_env(user)
    env["QA_PRODUCT"] = product
    env["QA_TICKETS_ROOT"] = str(tickets_root)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(config.REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as exc:  # noqa: BLE001
        _log(job_id, f"[启动失败] {type(exc).__name__}: {exc}")
        _finish(job_id, "failed", -1)
        return {"ok": False, "job_id": job_id, "error": str(exc)}
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            _log(job_id, raw.rstrip("\n"))
        rc = proc.wait()
    except Exception as exc:  # noqa: BLE001
        _log(job_id, f"[读取输出异常] {type(exc).__name__}: {exc}")
        rc = proc.poll() if proc.poll() is not None else -1
    _mirror_after_run(work_root, username, job_id, product, sprint)
    status = "done" if rc == 0 else "failed"
    _finish(job_id, status, rc)
    if rc == 0:
        _queue_post_review(job_id, username, product, sprint, work_root)
    return {"ok": rc == 0, "job_id": job_id, "product": product, "sprint": sprint, "rc": rc}


@app.task(name="qa.review.run", queue="review")
def review(job_id: str, username: str, product: str, sprint: str, kind: str, ticket_dirs: list[str] | None = None) -> dict:
    _mark_running(job_id)
    _log(job_id, "worker 已领取强模型任务。")
    user = auth.store.get(username) if username and username != "-" else None
    work_root = _work_root_for_run(job_id)
    if work_root is not None:
        config.set_tickets_root(work_root / "tickets")
    else:
        config.set_tickets_root(config.user_tickets_dir(username) if username and username != "-" else config.TICKETS_DIR)
    endpoint = user_anthropic_endpoint(user)

    async def _run() -> None:
        from webapp.strong import runner

        runner.set_endpoint(endpoint)

        def on_log(message: str) -> None:
            _log(job_id, message)

        dirs = list(ticket_dirs or [])
        if kind == "spot-check":
            from webapp.strong import spot_check
            _log(job_id, f"开始复核 {len(dirs)} 单…")
            await spot_check.run(dirs, product, on_log)
        elif kind == "finalize":
            from webapp.strong import repair, spot_check
            _log(job_id, f"开始定稿终检 {len(dirs)} 单（结构修复 + 语义复核）…")
            await repair.run(dirs, product, on_log)
            await spot_check.run(dirs, product, on_log)
        elif kind == "resolve":
            from webapp.strong import resolve
            _log(job_id, f"开始预答 {len(dirs)} 单…")
            await resolve.run(dirs, product, on_log)
        elif kind == "review":
            from webapp.strong import draft_review, resolve
            _log(job_id, f"开始预答 + 草稿复核 {len(dirs)} 单…")
            await resolve.run(dirs, product, on_log)
            await draft_review.run(dirs, product, on_log)
        elif kind == "kb-extract":
            from webapp.strong import kb_extract
            _log(job_id, f"开始提炼可入库规则 {len(dirs)} 单…")
            await kb_extract.run(dirs, product, on_log)
        else:
            raise ValueError(f"未知强模型作业：{kind}")

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        _log(job_id, f"[任务失败] {type(exc).__name__}: {exc}")
        _mirror_after_run(work_root, username, job_id, product, sprint)
        _finish(job_id, "failed", -1)
        return {"ok": False, "job_id": job_id, "error": str(exc)}
    _mirror_after_run(work_root, username, job_id, product, sprint)
    _finish(job_id, "done", 0)
    return {"ok": True, "job_id": job_id, "kind": kind}


@app.task(name="qa.maintenance.sweep_stalled", queue="maintenance", queueing_lock="maintenance:sweep_stalled")
def sweep_stalled(max_age_minutes: int = 60) -> dict:
    older_than = datetime.utcnow() - timedelta(minutes=max_age_minutes)
    updated = _repo_call(lambda repo: repo.mark_stalled(older_than=older_than)) or 0
    return {"ok": True, "max_age_minutes": max_age_minutes, "updated": updated}


def defer_generation(*, job_id: str, username: str, product: str, sprint: str, args: list[str] | None = None):
    return run_sprint.configure(queueing_lock=lock_key(username, product)).defer(
        job_id=job_id, username=username, product=product, sprint=sprint, args=args or []
    )


def defer_review(*, job_id: str, username: str, product: str, sprint: str, kind: str, ticket_dirs: list[str] | None = None):
    return review.configure(queueing_lock=lock_key(username, product)).defer(
        job_id=job_id, username=username, product=product, sprint=sprint, kind=kind, ticket_dirs=ticket_dirs or []
    )
