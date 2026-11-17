"""删除条目的校验与冲突判定规则。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import Finding, finding
from .models import DataFileSpec, DeleteFileSpec, DeleteOperation, RowRecord, TableSchema
from .schema import coerce_value


@dataclass(frozen=True)
class SpecState:
    schema_ok: bool
    columns_ok: bool
    target_ok: bool
    stale: bool


def normalize_key(
    key: dict[str, Any], columns: tuple[str, ...], schema: TableSchema
) -> tuple[dict[str, Any] | None, str]:
    if set(key) != set(columns):
        return None, "等值键字段与声明列不一致"
    normalized: dict[str, Any] = {}
    for name in sorted(columns):
        column = schema.column(name)
        if column is None:
            return None, "等值列不在模式中"
        ok, value, detail = coerce_value(column, key[name])
        if not ok:
            return None, f"{name}: {detail}"
        normalized[name] = value
    return normalized, ""


def matching_rows(
    normalized: dict[str, Any],
    spec: DeleteFileSpec,
    rows: tuple[RowRecord, ...],
    sequences: dict[str, int],
) -> list[RowRecord]:
    result = []
    for row in rows:
        if spec.target_file_id and row.file_id != spec.target_file_id:
            continue
        if sequences.get(row.file_id, 1 << 30) > spec.sequence_number:
            continue
        if all(row.values.get(name) == value for name, value in normalized.items()):
            result.append(row)
    return result


def position_operation(
    spec: DeleteFileSpec,
    entry: dict[str, Any],
    line_no: int,
    reason: str,
    state: SpecState,
    data_by_id: dict[str, DataFileSpec],
    seen: set[tuple[str, int]],
    findings: list[Finding],
) -> DeleteOperation | None:
    entry_file = entry.get("file_id")
    if entry_file is not None and (not isinstance(entry_file, str) or not entry_file):
        findings.append(finding("DELETE_ENTRY_INVALID", "file_id 非法", spec.path, line_no))
        return None
    target = entry_file or spec.target_file_id
    row_pos = entry.get("row_pos")
    if not isinstance(row_pos, int) or isinstance(row_pos, bool) or row_pos < 0:
        findings.append(finding("DELETE_ENTRY_INVALID", "row_pos 非法", spec.path, line_no))
        return None
    conflict = ""
    if not target or target not in data_by_id:
        findings.append(
            finding("ORPHAN_DELETE_TARGET", "位置删除目标文件不存在", spec.path, line_no,
                    {"target_file_id": target})
        )
        conflict = "ORPHAN_DELETE_TARGET"
    elif spec.target_file_id and entry_file and entry_file != spec.target_file_id:
        findings.append(
            finding("ORPHAN_DELETE_TARGET", "条目目标与清单目标不一致", spec.path, line_no,
                    {"target_file_id": target, "manifest_target": spec.target_file_id})
        )
        conflict = "ORPHAN_DELETE_TARGET"
    if target in data_by_id:
        row_count = data_by_id[target].row_count
        if row_pos >= row_count and not conflict:
            findings.append(
                finding("OUT_OF_RANGE_POSITION", "位置删除越界", spec.path, line_no,
                        {"target_file_id": target, "row_pos": row_pos, "row_count": row_count})
            )
            conflict = "OUT_OF_RANGE_POSITION"
        marker = (target, row_pos)
        if marker in seen and not conflict:
            findings.append(
                finding("DUPLICATE_POSITION_DELETE", "同一目标位置被重复删除", spec.path,
                        line_no, {"target_file_id": target, "row_pos": row_pos})
            )
            conflict = "DUPLICATE_POSITION_DELETE"
        seen.add(marker)
    if not conflict and not state.schema_ok:
        conflict = "DELETE_SCHEMA_MISMATCH"
    elif not conflict and not state.target_ok:
        conflict = "ORPHAN_DELETE_TARGET"
    elif not conflict and state.stale and target == spec.target_file_id:
        conflict = "STALE_SEQUENCE"
    return DeleteOperation(
        spec.file_id, line_no, "position", spec.sequence_number, target, row_pos,
        None, reason, (), not conflict, conflict,
    )


def equality_operation(
    spec: DeleteFileSpec,
    entry: dict[str, Any],
    line_no: int,
    reason: str,
    schema: TableSchema,
    state: SpecState,
    findings: list[Finding],
) -> tuple[DeleteOperation | None, dict[str, Any] | None]:
    key = entry.get("key")
    if not isinstance(key, dict) or not key:
        findings.append(finding("DELETE_ENTRY_INVALID", "等值删除 key 非法", spec.path, line_no))
        return None, None
    conflict = ""
    if not state.schema_ok:
        conflict = "DELETE_SCHEMA_MISMATCH"
    elif not state.columns_ok:
        conflict = "AMBIGUOUS_EQUALITY_COLUMNS"
    elif not state.target_ok:
        conflict = "ORPHAN_DELETE_TARGET"
    elif state.stale:
        conflict = "STALE_SEQUENCE"
    if conflict:
        return DeleteOperation(
            spec.file_id, line_no, "equality", spec.sequence_number, spec.target_file_id,
            None, None, reason, spec.equality_columns, False, conflict,
        ), None
    if set(key) != set(spec.equality_columns):
        findings.append(
            finding("AMBIGUOUS_EQUALITY_COLUMNS", "等值键字段与声明列不一致", spec.path,
                    line_no, {"key_columns": sorted(key),
                              "equality_columns": list(spec.equality_columns)})
        )
        return DeleteOperation(
            spec.file_id, line_no, "equality", spec.sequence_number, spec.target_file_id,
            None, None, reason, spec.equality_columns, False, "AMBIGUOUS_EQUALITY_COLUMNS",
        ), None
    normalized, message = normalize_key(key, spec.equality_columns, schema)
    if normalized is None:
        findings.append(
            finding("DELETE_ENTRY_INVALID", f"等值键值非法: {message}", spec.path, line_no)
        )
        return None, None
    return DeleteOperation(
        spec.file_id, line_no, "equality", spec.sequence_number, spec.target_file_id,
        None, normalized, reason, spec.equality_columns, True, "",
    ), normalized
