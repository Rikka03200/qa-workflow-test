#!/usr/bin/env python
"""Run a lightweight SSE concurrency check against an existing qa-workflow job.

The script logs in with credentials supplied via environment variables, opens N
concurrent `/jobs/{id}/events` streams, checks `/healthz` while streams are open,
and writes only aggregate latency/status metrics to `deploy/reports/`.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import os
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path

DEFAULT_REPORT_DIR = Path(__file__).resolve().parent / "reports"


class LoadCheckError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open concurrent job SSE streams and verify Web remains responsive.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8800", help="Web base URL")
    parser.add_argument("--job-id", required=True, help="Existing job id to stream")
    parser.add_argument("--username-env", default="QA_LOAD_USERNAME", help="Environment variable containing username")
    parser.add_argument("--password-env", default="QA_LOAD_PASSWORD", help="Environment variable containing password")
    parser.add_argument("--connections", type=int, default=50, help="Concurrent SSE connections")
    parser.add_argument("--duration", type=float, default=15.0, help="Seconds to keep streams open before cancelling")
    parser.add_argument("--connect-timeout", type=float, default=10.0, help="Per-stream connection timeout")
    parser.add_argument("--p95-threshold-ms", type=float, default=1500.0, help="Fail if healthz P95 exceeds this value")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR, help="Directory for markdown/json reports")
    return parser


def opener_with_login(base_url: str, username: str, password: str) -> tuple[urllib.request.OpenerDirector, dict[str, str]]:
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(base_url.rstrip("/") + "/login", timeout=10) as response:
        response.read()
    csrf = ""
    for cookie in jar:
        if cookie.name == "qa_csrf":
            csrf = cookie.value
            break
    data = urllib.parse.urlencode({"username": username, "password": password, "csrf_token": csrf}).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/login",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf, "Cookie": f"qa_csrf={csrf}"},
        method="POST",
    )
    with opener.open(request, timeout=10) as response:
        response.read()
    headers = {"Cookie": "; ".join(f"{cookie.name}={cookie.value}" for cookie in jar)}
    if "qa_session=" not in headers["Cookie"]:
        raise LoadCheckError("login did not produce qa_session cookie")
    return opener, headers


def healthz_latency(opener: urllib.request.OpenerDirector, base_url: str) -> tuple[float, bool]:
    start = time.monotonic()
    try:
        with opener.open(base_url.rstrip("/") + "/healthz", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return (time.monotonic() - start) * 1000, bool(payload.get("ok"))
    except Exception:
        return (time.monotonic() - start) * 1000, False


def connect_sse(index: int, base_url: str, job_id: str, cookie_header: str, duration: float, connect_timeout: float) -> dict:
    url = base_url.rstrip("/") + f"/jobs/{urllib.parse.quote(job_id)}/events"
    start = time.monotonic()
    try:
        request = urllib.request.Request(url, headers={"Cookie": cookie_header, "Accept": "text/event-stream"})
        with urllib.request.urlopen(request, timeout=connect_timeout) as response:
            connected_ms = (time.monotonic() - start) * 1000
            content_type = response.headers.get("content-type", "")
            time.sleep(max(0.0, duration))
            return {"index": index, "ok": "text/event-stream" in content_type, "connect_ms": connected_ms}
    except Exception as exc:  # noqa: BLE001
        return {"index": index, "ok": False, "connect_ms": (time.monotonic() - start) * 1000, "error": f"{type(exc).__name__}: {exc}"}


async def run_load(args: argparse.Namespace, opener: urllib.request.OpenerDirector, cookie_header: str) -> dict:
    warm_ms, warm_ok = healthz_latency(opener, args.base_url)
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.connections)
    tasks = [
        loop.run_in_executor(executor, connect_sse, i, args.base_url, args.job_id, cookie_header, args.duration, args.connect_timeout)
        for i in range(args.connections)
    ]
    await asyncio.sleep(0.5)
    during_samples = [healthz_latency(opener, args.base_url) for _ in range(5)]
    results = await asyncio.gather(*tasks)
    executor.shutdown(wait=True)
    after_ms, after_ok = healthz_latency(opener, args.base_url)
    connect_times = [item["connect_ms"] for item in results if item.get("ok")]
    health_times = [warm_ms, after_ms] + [sample[0] for sample in during_samples]
    p95 = statistics.quantiles(health_times, n=20)[18] if len(health_times) >= 2 else health_times[0]
    return {
        "connections": args.connections,
        "duration_seconds": args.duration,
        "ok_connections": sum(1 for item in results if item.get("ok")),
        "failed_connections": sum(1 for item in results if not item.get("ok")),
        "connect_ms_max": round(max(connect_times), 2) if connect_times else None,
        "connect_ms_p95": round(statistics.quantiles(connect_times, n=20)[18], 2) if len(connect_times) >= 2 else (round(connect_times[0], 2) if connect_times else None),
        "healthz_ok": warm_ok and after_ok and all(sample[1] for sample in during_samples),
        "healthz_ms_p95": round(p95, 2),
        "healthz_ms_samples": [round(v, 2) for v in health_times],
        "errors": [item.get("error") for item in results if item.get("error")][:10],
    }


def write_reports(report: dict, report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["started_at"].replace(":", "").replace("-", "")
    json_path = report_dir / f"sse-load-check-{stamp}.json"
    md_path = report_dir / f"sse-load-check-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = report.get("metrics", {})
    lines = [
        "# SSE Load Check Report",
        "",
        f"- status: `{report['status']}`",
        f"- base_url: `{report['base_url']}`",
        f"- job_id: `{report['job_id']}`",
        f"- started_at: `{report['started_at']}`",
        f"- finished_at: `{report.get('finished_at', '')}`",
        f"- connections: `{metrics.get('connections')}`",
        f"- ok_connections: `{metrics.get('ok_connections')}`",
        f"- failed_connections: `{metrics.get('failed_connections')}`",
        f"- healthz_ms_p95: `{metrics.get('healthz_ms_p95')}`",
        f"- connect_ms_p95: `{metrics.get('connect_ms_p95')}`",
    ]
    if report.get("error"):
        lines += ["", "## Error", "", f"`{report['error']}`"]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    username = os.environ.get(args.username_env, "")
    password = os.environ.get(args.password_env, "")
    if not username or not password:
        raise SystemExit(f"Set {args.username_env} and {args.password_env} before running the load check.")
    if args.connections <= 0:
        raise SystemExit("--connections must be positive")

    report: dict = {
        "kind": "sse-load-check",
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": args.base_url.rstrip("/"),
        "job_id": args.job_id,
        "p95_threshold_ms": args.p95_threshold_ms,
    }
    return_code = 0
    try:
        opener, headers = opener_with_login(args.base_url, username, password)
        metrics = asyncio.run(run_load(args, opener, headers["Cookie"]))
        report["metrics"] = metrics
        if metrics["failed_connections"]:
            raise LoadCheckError(f"{metrics['failed_connections']} SSE connections failed")
        if not metrics["healthz_ok"]:
            raise LoadCheckError("healthz failed while SSE streams were open")
        if metrics["healthz_ms_p95"] > args.p95_threshold_ms:
            raise LoadCheckError(f"healthz P95 exceeded threshold: {metrics['healthz_ms_p95']}ms > {args.p95_threshold_ms}ms")
        report["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        return_code = 1
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _, md_path = write_reports(report, args.report_dir)
        print(f"sse_load_check={report['status']} report={md_path}")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
