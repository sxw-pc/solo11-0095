"""审计编排：加载、删除解析、可见性、对账与稳定统计。"""

from __future__ import annotations

from pathlib import Path

from .delete_vectors import load_delete_vectors
from .errors import Finding, sort_findings
from .models import AuditResult
from .reconcile import reconcile
from .util import FIXED_RUN_AT, canonical_json, sha256_text, validate_run_at
from .visibility import compute_visibility
from .workspace import load_workspace

RULES_VERSION = "MOR-1.0"


def execute_audit(
    workspace: str | Path,
    snapshot_id: str | None = None,
    run_at: str = FIXED_RUN_AT,
) -> AuditResult:
    validated_run_at = validate_run_at(run_at)
    data = load_workspace(workspace, snapshot_id)
    operations, delete_findings = load_delete_vectors(
        data.root,
        data.snapshot.delete_files,
        data.table.schema,
        data.snapshot.data_files,
        data.rows,
    )
    decisions, reason_counts = compute_visibility(
        data.rows, data.snapshot.data_files, operations
    )
    findings: list[Finding] = list(data.findings) + list(delete_findings)
    diffs, diff_summary = reconcile(data.rows, decisions, data.expected, findings)
    ordered = sort_findings(findings)
    visible = sum(1 for item in decisions if item.visible)
    stats = {
        "data_files": len(data.snapshot.data_files),
        "delete_files": len(data.snapshot.delete_files),
        "delete_operations": len(operations),
        "applicable_deletes": sum(1 for item in operations if item.applicable),
        "rows": len(data.rows),
        "visible_rows": visible,
        "hidden_rows": len(data.rows) - visible,
        "expected_rows": len(data.expected),
        "findings": len(ordered),
        "finding_errors": sum(1 for item in ordered if item.severity == "error"),
        "finding_warnings": sum(1 for item in ordered if item.severity == "warning"),
        "diffs": len(diffs),
    }
    run_key = sha256_text(
        canonical_json(
            {
                "workspace_fingerprint": data.fingerprint,
                "snapshot_id": data.snapshot.snapshot_id,
                "rule_version": data.table.rule_version,
            }
        )
    )
    return AuditResult(
        data,
        operations,
        decisions,
        diffs,
        diff_summary,
        tuple(ordered),
        stats,
        reason_counts,
        run_key,
        validated_run_at,
        data.table.rule_version,
    )
