"""SQLite 审计台账、幂等写入与完整性检查。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .errors import AuditError
from .models import AuditResult
from .util import canonical_json, sha256_file

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS runs (
  run_key TEXT PRIMARY KEY,
  workspace_fingerprint TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  run_at TEXT NOT NULL,
  status TEXT NOT NULL,
  stats_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_key TEXT NOT NULL REFERENCES runs(run_key) ON DELETE CASCADE,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  format TEXT NOT NULL,
  UNIQUE(run_key, path)
);
CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_key TEXT NOT NULL REFERENCES runs(run_key) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  code TEXT NOT NULL,
  severity TEXT NOT NULL,
  file TEXT NOT NULL,
  row INTEGER,
  message TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  UNIQUE(run_key, ordinal)
);
CREATE TABLE IF NOT EXISTS row_visibility (
  run_key TEXT NOT NULL REFERENCES runs(run_key) ON DELETE CASCADE,
  file_id TEXT NOT NULL,
  row_pos INTEGER NOT NULL,
  row_id TEXT,
  visible INTEGER NOT NULL,
  reason_code TEXT NOT NULL,
  decision_chain_json TEXT NOT NULL,
  PRIMARY KEY(run_key, file_id, row_pos)
);
CREATE TABLE IF NOT EXISTS diff_summary (
  run_key TEXT NOT NULL REFERENCES runs(run_key) ON DELETE CASCADE,
  classification TEXT NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY(run_key, classification)
);
"""


def _artifact_path(path: str | Path, db_dir: Path) -> str:
    absolute = Path(path).resolve()
    try:
        return absolute.relative_to(db_dir.resolve()).as_posix()
    except ValueError:
        return Path(os.path.relpath(absolute, db_dir.resolve())).as_posix()


def write_ledger(
    db_path: str | Path,
    result: AuditResult,
    artifacts: list[dict[str, Any]],
) -> Path:
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    db_dir = db.parent
    connection = sqlite3.connect(str(db))
    try:
        connection.executescript(SCHEMA)
        status = "completed_with_diff" if result.stats["diffs"] else "passed"
        with connection:
            connection.execute(
                """
                INSERT INTO runs(run_key, workspace_fingerprint, snapshot_id,
                                 rule_version, run_at, status, stats_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key) DO UPDATE SET
                  workspace_fingerprint=excluded.workspace_fingerprint,
                  snapshot_id=excluded.snapshot_id,
                  rule_version=excluded.rule_version,
                  run_at=excluded.run_at,
                  status=excluded.status,
                  stats_json=excluded.stats_json
                """,
                (
                    result.run_key,
                    result.workspace.fingerprint,
                    result.workspace.snapshot.snapshot_id,
                    result.rule_version,
                    result.run_at,
                    status,
                    canonical_json(result.stats),
                ),
            )
            for table in ("artifacts", "findings", "row_visibility", "diff_summary"):
                connection.execute(f"DELETE FROM {table} WHERE run_key = ?", (result.run_key,))
            for artifact in artifacts:
                path = Path(artifact["path"])
                connection.execute(
                    "INSERT INTO artifacts(run_key, path, sha256, format) VALUES (?, ?, ?, ?)",
                    (
                        result.run_key,
                        _artifact_path(path, db_dir),
                        sha256_file(path),
                        artifact["format"],
                    ),
                )
            for ordinal, item in enumerate(result.findings, start=1):
                connection.execute(
                    """INSERT INTO findings(run_key, ordinal, code, severity, file, row,
                                            message, evidence_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        result.run_key,
                        ordinal,
                        item.code,
                        item.severity,
                        item.file,
                        item.row,
                        item.message,
                        canonical_json(item.evidence),
                    ),
                )
            for decision in result.decisions:
                connection.execute(
                    """INSERT INTO row_visibility(run_key, file_id, row_pos, row_id,
                                                  visible, reason_code, decision_chain_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        result.run_key,
                        decision.file_id,
                        decision.row_pos,
                        decision.row_id,
                        1 if decision.visible else 0,
                        decision.reason_code,
                        canonical_json(list(decision.chain)),
                    ),
                )
            for classification, count in sorted(result.diff_summary.items()):
                connection.execute(
                    "INSERT INTO diff_summary(run_key, classification, count) VALUES (?, ?, ?)",
                    (result.run_key, classification, count),
                )
    finally:
        connection.close()
    return db


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def verify_ledger(db_path: str | Path) -> dict[str, Any]:
    db = Path(db_path)
    if not db.is_file():
        raise AuditError("LEDGER_CHECK_FAILED", f"台账不存在: {db}", str(db))
    try:
        connection = sqlite3.connect(str(db))
    except sqlite3.Error as exc:
        raise AuditError("LEDGER_CHECK_FAILED", f"无法打开台账: {exc}", str(db)) from exc
    connection.row_factory = sqlite3.Row
    checks: list[dict[str, Any]] = []
    try:
        table_names = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {"runs", "artifacts", "findings", "row_visibility", "diff_summary"}
        checks.append(_check("tables", required <= table_names, ",".join(sorted(required))))
        fk = list(connection.execute("PRAGMA foreign_key_check"))
        checks.append(_check("foreign_keys", not fk, f"{len(fk)} violation(s)"))
        runs = list(connection.execute("SELECT * FROM runs ORDER BY run_key"))
        checks.append(_check("runs_present", bool(runs), f"{len(runs)} run(s)"))
        duplicate = list(
            connection.execute(
                """SELECT workspace_fingerprint, snapshot_id, rule_version,
                          COUNT(*) AS n
                   FROM runs GROUP BY 1,2,3 HAVING COUNT(*) > 1"""
            )
        )
        checks.append(_check("duplicate_runs", not duplicate, f"{len(duplicate)} group(s)"))
        for run in runs:
            checks.extend(_verify_run(connection, run))
    except sqlite3.Error as exc:
        raise AuditError("LEDGER_CHECK_FAILED", f"台账检查失败: {exc}", str(db)) from exc
    finally:
        connection.close()
    ok = all(item["ok"] for item in checks)
    return {
        "ok": ok,
        "database": str(db),
        "runs": len(runs) if "runs" in locals() else 0,
        "checks": checks,
    }


def _verify_run(connection: sqlite3.Connection, run: sqlite3.Row) -> list[dict[str, Any]]:
    key = run["run_key"]
    try:
        stats = json.loads(run["stats_json"])
    except json.JSONDecodeError:
        return [_check(f"run:{key}:stats_json", False, "invalid JSON")]
    rows = connection.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(visible),0) v FROM row_visibility WHERE run_key=?",
        (key,),
    ).fetchone()
    findings = connection.execute(
        "SELECT COUNT(*) n FROM findings WHERE run_key=?", (key,)
    ).fetchone()["n"]
    diff_rows = connection.execute(
        "SELECT classification, count FROM diff_summary WHERE run_key=?", (key,)
    ).fetchall()
    counts = {row["classification"]: row["count"] for row in diff_rows}
    diff_total = counts.get("total", 0)
    diff_parts = sum(
        count for classification, count in counts.items() if classification != "total"
    )
    checks = [
        _check(
            f"run:{key}:diff_parts",
            diff_parts == diff_total,
            f"{diff_parts}/{diff_total}",
        ),
        _check(f"run:{key}:rows", rows["n"] == stats.get("rows"), f"{rows['n']}"),
        _check(
            f"run:{key}:visible",
            rows["v"] == stats.get("visible_rows"),
            f"{rows['v']}",
        ),
        _check(
            f"run:{key}:findings", findings == stats.get("findings"), f"{findings}"
        ),
        _check(
            f"run:{key}:diffs", diff_total == stats.get("diffs"), f"{diff_total}",
        ),
    ]
    artifacts = list(
        connection.execute(
            "SELECT path, sha256 FROM artifacts WHERE run_key=? ORDER BY path", (key,)
        )
    )
    checks.append(
        _check(f"run:{key}:artifacts", bool(artifacts), f"{len(artifacts)} artifact(s)")
    )
    for artifact in artifacts:
        path = Path(artifact["path"])
        if not path.is_absolute():
            path = connection_path(connection, path)
        exists = path.is_file()
        matched = exists and sha256_file(path) == artifact["sha256"]
        checks.append(
            _check(f"run:{key}:artifact:{artifact['path']}", matched,
                   "hash ok" if matched else "missing or hash mismatch")
        )
    return checks


def connection_path(connection: sqlite3.Connection, relative: Path) -> Path:
    row = connection.execute("PRAGMA database_list").fetchone()
    database = Path(row[2]) if row and row[2] else Path(".")
    return (database.parent / relative).resolve()
