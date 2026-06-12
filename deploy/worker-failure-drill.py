#!/usr/bin/env python
"""Run a reversible worker failure drill for a qa-workflow Compose stack.

By default this script only writes a planned drill report. Pass `--execute` to
stop one worker service, verify that Web `/healthz` remains reachable, restart
the worker, and wait for it to return to healthy/running state.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent
DEFAULT_ENV = DEPLOY_DIR / "env" / "local.env"
DEFAULT_COMPOSE = DEPLOY_DIR / "compose.yaml"
DEFAULT_REPORT_DIR = DEPLOY_DIR / "reports"
ALLOWED_WORKERS = {"worker-gen", "worker-review", "worker"}
SECRET_RE = re.compile(r"(?i)(password|token|secret|key)(\s*[=:]\s*)([^\s&;,]+)")


class DrillError(RuntimeError):
    pass


def redact(text: str) -> str:
    return SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text or "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stop and restart a Compose worker service to verify failure recovery.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV, help="Compose env file; default: deploy/env/local.env")
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE, help="Compose file; default: deploy/compose.yaml")
    parser.add_argument("--project-name", default="", help="Optional Docker Compose project name")
    parser.add_argument("--service", default="worker-gen", choices=sorted(ALLOWED_WORKERS), help="Worker service to stop/restart")
    parser.add_argument("--web-url", default="http://127.0.0.1:8800", help="External Web URL for /healthz checks")
    parser.add_argument("--stop-seconds", type=float, default=10.0, help="Seconds to keep the worker stopped during the drill")
    parser.add_argument("--wait-seconds", type=float, default=90.0, help="Max seconds to wait for worker recovery")
    parser.add_argument("--execute", action="store_true", help="Actually stop and restart the selected worker")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR, help="Directory for markdown/json drill reports")
    return parser


def compose_base(args: argparse.Namespace) -> list[str]:
    cmd = ["docker", "compose", "--env-file", str(args.env_file), "-f", str(args.compose_file)]
    if args.project_name:
        cmd += ["--project-name", args.project_name]
    return cmd


def run_step(report: dict, name: str, cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    start = time.monotonic()
    proc = subprocess.run(cmd, cwd=DEPLOY_DIR, text=True, capture_output=True)
    elapsed = round(time.monotonic() - start, 3)
    output = redact((proc.stdout or "") + (proc.stderr or ""))
    report.setdefault("steps", []).append({
        "name": name,
        "returncode": proc.returncode,
        "elapsed_seconds": elapsed,
        "output_tail": output.splitlines()[-30:],
    })
    if check and proc.returncode != 0:
        raise DrillError(f"{name} failed with exit code {proc.returncode}")
    return proc


def check_web_health(report: dict, web_url: str, label: str) -> dict:
    start = time.monotonic()
    url = web_url.rstrip("/") + "/healthz"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        status = {"label": label, "ok": bool(payload.get("ok")), "http": 200, "elapsed_seconds": round(time.monotonic() - start, 3)}
        status["queue_depth_warn"] = bool(payload.get("queue_depth_warn"))
        status["queue_depth"] = payload.get("queue_depth") or {}
    except Exception as exc:  # noqa: BLE001
        status = {"label": label, "ok": False, "error": redact(f"{type(exc).__name__}: {exc}"), "elapsed_seconds": round(time.monotonic() - start, 3)}
    report.setdefault("healthz", []).append(status)
    return status


def worker_container_id(args: argparse.Namespace, report: dict) -> str:
    proc = run_step(report, "get worker container id", compose_base(args) + ["ps", "-q", args.service])
    cid = (proc.stdout or "").strip().splitlines()[0] if proc.stdout.strip() else ""
    if not cid:
        raise DrillError(f"worker service is not running: {args.service}")
    return cid


def worker_state(args: argparse.Namespace, report: dict, label: str) -> dict:
    cid = worker_container_id(args, report)
    proc = run_step(
        report,
        label,
        ["docker", "inspect", "--format", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}", cid],
        check=False,
    )
    text = (proc.stdout or "").strip()
    parts = text.split()
    state = {"label": label, "container": cid[:12], "status": parts[0] if parts else "unknown", "health": parts[1] if len(parts) > 1 else "unknown"}
    report.setdefault("worker_state", []).append(state)
    return state


def wait_for_worker(args: argparse.Namespace, report: dict) -> dict:
    deadline = time.monotonic() + args.wait_seconds
    last = {}
    while time.monotonic() < deadline:
        last = worker_state(args, report, "poll worker state")
        if last.get("status") == "running" and last.get("health") in {"healthy", "no-healthcheck"}:
            return last
        time.sleep(3)
    raise DrillError(f"worker did not recover before timeout: {last}")


def write_reports(report: dict, report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["started_at"].replace(":", "").replace("-", "")
    json_path = report_dir / f"worker-failure-drill-{stamp}.json"
    md_path = report_dir / f"worker-failure-drill-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Worker Failure Drill Report",
        "",
        f"- status: `{report['status']}`",
        f"- mode: `{'execute' if report.get('executed') else 'plan-only'}`",
        f"- service: `{report['service']}`",
        f"- started_at: `{report['started_at']}`",
        f"- finished_at: `{report.get('finished_at', '')}`",
        f"- web_url: `{report['web_url']}`",
        "",
        "## Health Checks",
    ]
    for item in report.get("healthz", []):
        lines.append(f"- `{item.get('label')}` ok={item.get('ok')} elapsed={item.get('elapsed_seconds')}s queue_depth_warn={item.get('queue_depth_warn')}")
    lines += ["", "## Steps"]
    for step in report.get("steps", []):
        lines.append(f"- `{step['name']}` rc={step['returncode']} elapsed={step['elapsed_seconds']}s")
    if report.get("error"):
        lines += ["", "## Error", "", f"`{report['error']}`"]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.env_file = args.env_file.resolve()
    args.compose_file = args.compose_file.resolve()
    args.report_dir = args.report_dir.resolve()
    if not args.env_file.exists():
        raise SystemExit(f"env file not found: {args.env_file}")
    if not args.compose_file.exists():
        raise SystemExit(f"compose file not found: {args.compose_file}")

    report: dict = {
        "kind": "worker-failure-drill",
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "service": args.service,
        "web_url": args.web_url.rstrip("/"),
        "stop_seconds": args.stop_seconds,
        "executed": bool(args.execute),
    }
    return_code = 0
    try:
        if not args.execute:
            report["status"] = "planned"
            report["note"] = "Run again with --execute to stop and restart the worker service."
        else:
            worker_state(args, report, "initial worker state")
            before = check_web_health(report, args.web_url, "before stop")
            if not before.get("ok"):
                raise DrillError("web healthz failed before worker stop")
            run_step(report, "stop worker", compose_base(args) + ["stop", args.service])
            time.sleep(max(0.0, args.stop_seconds))
            during = check_web_health(report, args.web_url, "while worker stopped")
            if not during.get("ok"):
                raise DrillError("web healthz failed while worker stopped")
            run_step(report, "restart worker", compose_base(args) + ["up", "-d", args.service])
            recovered = wait_for_worker(args, report)
            after = check_web_health(report, args.web_url, "after restart")
            if not after.get("ok"):
                raise DrillError("web healthz failed after worker restart")
            report["recovered_state"] = recovered
            report["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["error"] = redact(f"{type(exc).__name__}: {exc}")
        return_code = 1
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _, md_path = write_reports(report, args.report_dir)
        print(f"worker_failure_drill={report['status']} report={md_path}")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
