"""审计编排：加载、删除解析、完整性证明、可见性、对账与稳定统计。"""

from __future__ import annotations

from pathlib import Path

from .delete_vectors import load_delete_vectors
from .errors import Finding, sort_findings
from .models import AuditResult, SourceIntegrity
from .reconcile import reconcile, reconcile_certainty
from .util import FIXED_RUN_AT, canonical_json, sha256_text, validate_run_at
from .visibility import certainty_counts, compute_visibility
from .workspace import load_workspace

RULES_VERSION = "MOR-1.0"


def execute_audit(
    workspace: str | Path,
    snapshot_id: str | None = None,
    run_at: str = FIXED_RUN_AT,
) -> AuditResult:
    validated_run_at = validate_run_at(run_at)
    data = load_workspace(workspace, snapshot_id)
    delete_states: list[SourceIntegrity] = []
    operations, delete_findings = load_delete_vectors(
        data.root,
        data.snapshot.delete_files,
        data.table.schema,
        data.snapshot.data_files,
        data.rows,
        delete_states,
    )
    decisions, reason_counts = compute_visibility(
        data.rows,
        data.snapshot.data_files,
        operations,
        data.snapshot.delete_files,
        tuple(delete_states),
    )
    findings: list[Finding] = list(data.findings) + list(delete_findings)
    diffs, diff_summary = reconcile(data.rows, decisions, data.expected, findings)
    data_trusted = all(item.trusted for item in data.data_integrity)
    delete_trusted = all(item.trusted for item in delete_states)
    outcome = reconcile_certainty(
        data.rows,
        decisions,
        data.expected,
        diffs,
        data_trusted,
        delete_trusted,
    )
    ordered = sort_findings(findings)
    visible = sum(1 for item in decisions if item.visible)
    # 三态统计必须守恒；此处即最早暴露不一致的位置，报告与台账复用同一批决定。
    certainty = certainty_counts(decisions)
    if (
        certainty["certain_visible"]
        + certainty["certain_hidden"]
        + certainty["indeterminate"]
        != len(decisions)
        or certainty["provisional_visible"] != visible
        or certainty["provisional_hidden"] != len(decisions) - visible
    ):
        raise AssertionError("certainty counts are not conserved")
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
    # 对账结论由同一批完整性状态驱动，而不是只做报告层 warning。
    certainty["publishable"] = 1 if outcome.publishable else 0
    certainty["certainty_conserved"] = 1
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
        tuple(delete_states),
        outcome,
    )
