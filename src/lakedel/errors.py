"""错误码、异常与结构化 finding。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


class AuditError(Exception):
    """可预期的输入、路径或台账错误，对应退出码 3。"""

    def __init__(self, code: str, message: str, path: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


WARNING_CODES = {
    "BLANK_LINE",
    "DUPLICATE_POSITION_DELETE",
    "DUPLICATE_EQUALITY_DELETE",
    "DUPLICATE_ROW_ID",
    "EQUALITY_NO_MATCH",
    "ORPHAN_DELETE_TARGET",
    "OUT_OF_RANGE_POSITION",
    "STALE_SEQUENCE",
    "AMBIGUOUS_EQUALITY_COLUMNS",
}

ERROR_CODES = {
    "AMBIGUOUS_EQUALITY_COLUMNS",
    "CLI_USAGE",
    "DELETE_ENTRY_INVALID",
    "DELETE_SCHEMA_MISMATCH",
    "DUPLICATE_COLUMN",
    "DUPLICATE_EQUALITY_DELETE",
    "DUPLICATE_EXPECTED_ROW",
    "DUPLICATE_ID",
    "DUPLICATE_POSITION_DELETE",
    "DUPLICATE_ROW_ID",
    "DUPLICATE_ROW_POSITION",
    "EQUALITY_NO_MATCH",
    "EXPECTED_INVALID",
    "EXTRA_COLUMN",
    "EXTRA_EXPECTED_ROW",
    "FILE_DECODE_ERROR",
    "FILE_HASH_MISMATCH",
    "INVALID_JSON_LINE",
    "INVALID_ROW",
    "INVALID_VALUE",
    "JSON_INVALID",
    "LEDGER_CHECK_FAILED",
    "MANIFEST_MISSING_FIELD",
    "MANIFEST_TYPE_ERROR",
    "MISSING_COLUMN",
    "MISSING_EXPECTED_ROW",
    "MISSING_VISIBLE",
    "ORPHAN_DELETE_TARGET",
    "OUT_OF_RANGE_POSITION",
    "PATH_ESCAPE",
    "PATH_MISSING",
    "ROW_COUNT_MISMATCH",
    "ROW_NOT_FOUND",
    "SCHEMA_HASH_MISMATCH",
    "SCHEMA_INVALID",
    "SNAPSHOT_NOT_FOUND",
    "STALE_SEQUENCE",
    "UNEXPECTED_VISIBLE",
    "UNKNOWN_DELETE_KIND",
    "UNKNOWN_FORMAT",
    "WORKSPACE_INVALID",
}


def severity_for(code: str) -> str:
    return "warning" if code in WARNING_CODES else "error"


@dataclass(frozen=True)
class Finding:
    code: str
    message: str
    file: str = ""
    row: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        return severity_for(self.code)

    def to_dict(self, ordinal: int | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "file": self.file,
            "row": self.row,
            "evidence": self.evidence,
        }
        if ordinal is not None:
            data["ordinal"] = ordinal
        return data


def finding(
    code: str,
    message: str,
    file: str = "",
    row: int | None = None,
    evidence: dict[str, Any] | None = None,
) -> Finding:
    return Finding(code, message, file, row, evidence or {})


def sort_findings(items: Iterable[Finding]) -> list[Finding]:
    return sorted(
        items,
        key=lambda f: (f.file, -1 if f.row is None else f.row, f.code, f.message),
    )


def finding_dicts(items: Iterable[Finding]) -> list[dict[str, Any]]:
    return [item.to_dict(i) for i, item in enumerate(sort_findings(items), start=1)]
