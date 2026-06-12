#!/usr/bin/env python
"""Run a PostgreSQL backup restore drill for a qa-workflow Compose stack.

The drill restores a dump from the Compose `backups` volume into a temporary
PostgreSQL database, verifies core platform tables, and writes a report under
`deploy/reports/`. It never drops the original application database.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = Path(__file__).resolve().parent
DEFAULT_ENV = DEPLOY_DIR / "env" / "local.env"
DEFAULT_COMPOSE = DEPLOY_DIR / "compose.yaml"
DEFAULT_REPORT_DIR = DEPLOY_DIR / "reports"
TEMP_DB_PREFIX = "qa_restore_drill_"
REQUIRED_TABLES = {
    "users",
    "sessions",
    "user_credentials",
    "products",
    "audit_events",
    "pipeline_runs",
    "pipeline_steps",
    "job_logs",
    "tickets",
    "artifacts",
    "coverage_ledger",
    "sprint_selections",
}
SECRET_RE = re.compile(r"(?i)(password|token|secret|key)(\s*[=:]\s*)([^\s&;,]+)")


class DrillError(RuntimeError):
    pass


def redact(text: str) -> str:
    return SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text or "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore the latest qa-workflow backup into a temporary DB and smoke-check it.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV, help="Compose env file; default: deploy/env/local.env")
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE, help="Compose file; default: deploy/compose.yaml")
    parser.add_argument("--project-name", default="", help="Optional Docker Compose project name")
    parser.add_argument("--dump", default="", help="Dump path inside the backups volume, e.g. /backups/qa_workflow-YYYY.dump; latest dump is used when omitted")
    parser.add_argument("--create-backup", action="store_true", help="Run the backup profile before restoring")
    parser.add_argument("--drop-temp-db-after", action="store_true", help="Drop only the generated temporary restore DB after a successful or failed drill")
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


def latest_dump(args: argparse.Namespace, report: dict) -> str:
    if args.dump:
        return args.dump if args.dump.startswith("/backups/") else f"/backups/{args.dump}"
    proc = run_step(
        report,
        "find latest dump",
        compose_base(args) + [
            "run", "--rm", "--entrypoint", "sh", "backup", "-c",
            "set -eu; found=; for f in /backups/*.dump; do [ -e \"$f\" ] || break; found=1; done; "
            "[ -n \"$found\" ]; ls -1t /backups/*.dump | sed -n '1p'",
        ],
    )
    dump = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else ""
    if not dump:
        raise DrillError("no backup dump found in the backups volume")
    return dump


def temporary_db_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{TEMP_DB_PREFIX}{stamp}"


def validate_temp_db_name(name: str) -> None:
    if not name.startswith(TEMP_DB_PREFIX) or not re.fullmatch(r"[a-zA-Z0-9_]+", name):
        raise DrillError(f"refusing to operate on non-drill database name: {name}")


def psql_tables(args: argparse.Namespace, report: dict, restore_db: str) -> set[str]:
    proc = run_step(
        report,
        "list restored tables",
        compose_base(args) + [
            "run", "--rm", "--entrypoint", "sh",
            "-e", f"RESTORE_DB={restore_db}",
            "backup", "-c",
            "psql -h postgres -U \"$POSTGRES_USER\" -d \"$RESTORE_DB\" -v ON_ERROR_STOP=1 -At "
            "-c \"select table_name from information_schema.tables where table_schema='public' order by table_name\"",
        ],
    )
    return {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}


def write_reports(report: dict, report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["started_at"].replace(":", "").replace("-", "")
    json_path = report_dir / f"restore-drill-{stamp}.json"
    md_path = report_dir / f"restore-drill-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Restore Drill Report",
        "",
        f"- status: `{report['status']}`",
        f"- started_at: `{report['started_at']}`",
        f"- finished_at: `{report.get('finished_at', '')}`",
        f"- dump: `{Path(report.get('dump', '')).name}`",
        f"- restore_db: `{report.get('restore_db', '')}`",
        f"- required_tables_ok: `{report.get('required_tables_ok', False)}`",
        f"- temp_db_dropped: `{report.get('temp_db_dropped', False)}`",
        "",
        "## Steps",
    ]
    for step in report.get("steps", []):
        lines.append(f"- `{step['name']}` rc={step['returncode']} elapsed={step['elapsed_seconds']}s")
    if report.get("error"):
        lines += ["", "## Error", "", f"`{report['error']}`"]
    if report.get("cleanup_command"):
        lines += ["", "## Manual Cleanup", "", "```bash", report["cleanup_command"], "```"]
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

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    restore_db = temporary_db_name()
    validate_temp_db_name(restore_db)
    report: dict = {
        "kind": "restore-drill",
        "status": "running",
        "started_at": started_at,
        "restore_db": restore_db,
        "required_tables": sorted(REQUIRED_TABLES),
        "temp_db_dropped": False,
    }
    drop_command = " ".join(compose_base(args) + ["exec", "-T", "postgres", "dropdb", "--if-exists", "-U", "${POSTGRES_USER}", restore_db])
    report["cleanup_command"] = redact(drop_command)

    try:
        if args.create_backup:
            run_step(report, "create backup", compose_base(args) + ["--profile", "backup", "run", "--rm", "backup"])
        dump = latest_dump(args, report)
        report["dump"] = dump
        run_step(report, "ensure postgres", compose_base(args) + ["up", "-d", "postgres"])
        run_step(report, "create temporary restore database", compose_base(args) + ["exec", "-T", "postgres", "sh", "-c", f"createdb -U \"$POSTGRES_USER\" {restore_db}"])
        run_step(
            report,
            "restore dump",
            compose_base(args) + [
                "run", "--rm", "--entrypoint", "sh",
                "-e", f"RESTORE_DB={restore_db}",
                "-e", f"DUMP_PATH={dump}",
                "backup", "-c",
                "pg_restore -h postgres -U \"$POSTGRES_USER\" -d \"$RESTORE_DB\" --no-owner --no-privileges \"$DUMP_PATH\"",
            ],
        )
        tables = psql_tables(args, report, restore_db)
        missing = sorted(REQUIRED_TABLES - tables)
        report["restored_tables"] = sorted(tables)
        report["missing_tables"] = missing
        report["required_tables_ok"] = not missing
        if missing:
            raise DrillError(f"missing restored tables: {missing}")
        report["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        report["status"] = "failed"
        report["error"] = redact(f"{type(exc).__name__}: {exc}")
        return_code = 1
    else:
        return_code = 0
    finally:
        if args.drop_temp_db_after:
            try:
                run_step(report, "drop temporary restore database", compose_base(args) + ["exec", "-T", "postgres", "sh", "-c", f"dropdb --if-exists -U \"$POSTGRES_USER\" {restore_db}"], check=False)
                report["temp_db_dropped"] = True
            except Exception as exc:  # noqa: BLE001
                report["drop_error"] = redact(f"{type(exc).__name__}: {exc}")
        report["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _, md_path = write_reports(report, args.report_dir)
        print(f"restore_drill={report['status']} report={md_path}")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
