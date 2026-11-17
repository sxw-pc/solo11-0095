"""领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import Finding


@dataclass(frozen=True)
class Column:
    name: str
    dtype: str
    nullable: bool
    ordinal: int


@dataclass(frozen=True)
class TableSchema:
    columns: tuple[Column, ...]
    schema_hash: str

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass(frozen=True)
class TableDef:
    table_id: str
    table_name: str
    rule_version: str
    created_at: str
    expected_visible: str
    schema: TableSchema


@dataclass(frozen=True)
class DataFileSpec:
    file_id: str
    path: str
    format: str
    row_count: int
    sha256: str
    sequence_number: int
    schema_hash: str


@dataclass(frozen=True)
class DeleteFileSpec:
    file_id: str
    path: str
    delete_kind: str
    sequence_number: int
    schema_hash: str
    sha256: str | None = None
    target_file_id: str | None = None
    equality_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    sequence_number: int
    schema_hash: str
    created_at: str
    data_files: tuple[DataFileSpec, ...]
    delete_files: tuple[DeleteFileSpec, ...]
    source: str = ""


@dataclass(frozen=True)
class SourceIntegrity:
    """单个输入文件相对清单声明的完整性状态。"""

    source_kind: str
    file_id: str
    path: str
    declared_sha256: str | None
    actual_sha256: str
    trusted: bool
    mismatch: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "file_id": self.file_id,
            "path": self.path,
            "declared_sha256": self.declared_sha256,
            "actual_sha256": self.actual_sha256,
            "trusted": self.trusted,
            "mismatch": self.mismatch,
            "status": "trusted" if self.trusted else "untrusted",
        }


@dataclass(frozen=True)
class RowRecord:
    file_id: str
    row_pos: int
    row_id: str | None
    values: dict[str, Any]
    source: str = ""
    source_trusted: bool = True
    source_actual_sha256: str | None = None
    source_declared_sha256: str | None = None


@dataclass(frozen=True)
class DeleteOperation:
    delete_file_id: str
    ordinal: int
    delete_kind: str
    sequence_number: int
    target_file_id: str | None
    row_pos: int | None
    key: dict[str, Any] | None
    reason: str
    equality_columns: tuple[str, ...] = ()
    applicable: bool = True
    conflict_code: str = ""
    source_trusted: bool = True
    source_actual_sha256: str | None = None
    source_declared_sha256: str | None = None


@dataclass(frozen=True)
class ExpectedRow:
    file_id: str
    row_pos: int
    row_id: str | None
    visible: bool


@dataclass(frozen=True)
class RowDecision:
    file_id: str
    row_pos: int
    row_id: str | None
    visible: bool
    reason_code: str
    position_hits: tuple[str, ...] = ()
    equality_hits: tuple[str, ...] = ()
    stale_hits: tuple[str, ...] = ()
    chain: tuple[dict[str, Any], ...] = ()
    certainty: str = "certain"
    certain_visible: bool = False
    certain_hidden: bool = False
    indeterminate: bool = False
    trusted_position_hits: tuple[str, ...] = ()
    trusted_equality_hits: tuple[str, ...] = ()
    suspect_position_hits: tuple[str, ...] = ()
    suspect_equality_hits: tuple[str, ...] = ()
    untrusted_equality_ids: tuple[str, ...] = ()
    untrusted_equality_rows: tuple[tuple[str, int], ...] = ()
    row_identity_trusted: bool = True
    untrusted_sources: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, int]:
        return (self.file_id, self.row_pos)

    @property
    def trusted_hits(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.trusted_position_hits + self.trusted_equality_hits)))

    @property
    def suspect_hits(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.suspect_position_hits + self.suspect_equality_hits)))

    @property
    def chain_summary(self) -> str:
        parts = [self.reason_code]
        if self.position_hits:
            parts.append("pos=" + ",".join(self.position_hits))
        if self.equality_hits:
            parts.append("eq=" + ",".join(self.equality_hits))
        if self.stale_hits:
            parts.append("stale=" + ",".join(self.stale_hits))
        if self.certainty != "certain":
            parts.append(f"certainty={self.certainty}")
        return "; ".join(parts)


@dataclass(frozen=True)
class ReconciliationOutcome:
    """面向发布核验与调查视图的两种明确授权结论。"""

    publishable: bool
    blockers: tuple[str, ...]
    indeterminate_rows: tuple[tuple[str, int], ...]
    expected_mismatches: tuple[tuple[str, int], ...]
    provisional_visible: int
    provisional_hidden: int
    certain_visible: int
    certain_hidden: int
    indeterminate: int

    @property
    def publish_reason(self) -> str:
        if self.publishable:
            return "PUBLISHABLE_CERTAIN_MATCH"
        if self.indeterminate_rows:
            return "NOT_PUBLISHABLE_INDETERMINATE"
        return "NOT_PUBLISHABLE_EXPECTED_MISMATCH"

    def to_dict(self) -> dict[str, Any]:
        return {
            "publishable": self.publishable,
            "publish_reason": self.publish_reason,
            "blockers": list(self.blockers),
            "indeterminate_rows": [list(item) for item in self.indeterminate_rows],
            "expected_mismatches": [list(item) for item in self.expected_mismatches],
            "provisional_visible": self.provisional_visible,
            "provisional_hidden": self.provisional_hidden,
            "certain_visible": self.certain_visible,
            "certain_hidden": self.certain_hidden,
            "indeterminate": self.indeterminate,
        }


@dataclass(frozen=True)
class DiffRecord:
    classification: str
    file_id: str
    row_pos: int
    row_id: str | None
    actual_visible: bool | None
    expected_visible: bool | None
    first_finding: str
    decision_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "file_id": self.file_id,
            "row_pos": self.row_pos,
            "row_id": self.row_id,
            "actual_visible": self.actual_visible,
            "expected_visible": self.expected_visible,
            "first_finding": self.first_finding,
            "decision_summary": self.decision_summary,
        }


@dataclass
class WorkspaceData:
    root: Path
    table: TableDef
    snapshots: tuple[Snapshot, ...]
    snapshot: Snapshot
    rows: tuple[RowRecord, ...]
    expected: tuple[ExpectedRow, ...]
    expected_path: Path
    fingerprint: str
    findings: list[Finding] = field(default_factory=list)
    data_integrity: tuple[SourceIntegrity, ...] = ()

    @property
    def integrity_by_file(self) -> dict[str, SourceIntegrity]:
        return {item.file_id: item for item in self.data_integrity}

    @property
    def has_untrusted_input(self) -> bool:
        return any(not item.trusted for item in self.data_integrity)


@dataclass
class AuditResult:
    workspace: WorkspaceData
    operations: tuple[DeleteOperation, ...]
    decisions: tuple[RowDecision, ...]
    diffs: tuple[DiffRecord, ...]
    diff_summary: dict[str, int]
    findings: tuple[Finding, ...]
    stats: dict[str, int]
    reason_counts: dict[str, int]
    run_key: str
    run_at: str
    rule_version: str
    delete_integrity: tuple[SourceIntegrity, ...] = ()
    outcome: ReconciliationOutcome | None = None
