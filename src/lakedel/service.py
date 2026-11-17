"""审计执行与报告/台账落盘的一站式服务。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .engine import execute_audit
from .ledger import write_ledger
from .models import AuditResult
from .report import write_reports
from .util import FIXED_RUN_AT


def produce_outputs(
    workspace: str | Path,
    out_dir: str | Path,
    formats: list[str],
    db_path: str | Path,
    snapshot_id: str | None = None,
    run_at: str = FIXED_RUN_AT,
    ledger_label: str | None = None,
) -> tuple[AuditResult, list[dict[str, Any]], Path]:
    result = execute_audit(workspace, snapshot_id, run_at)
    ledger = Path(db_path)
    artifacts = write_reports(
        out_dir, result, formats, str(ledger) if ledger_label is None else ledger_label
    )
    write_ledger(ledger, result, artifacts)
    return result, artifacts, ledger
