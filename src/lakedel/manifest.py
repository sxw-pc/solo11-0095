"""表、快照与文件清单解析。"""

from __future__ import annotations

import re
from typing import Any

from .errors import AuditError, Finding, finding
from .models import DataFileSpec, DeleteFileSpec, Snapshot, TableDef
from .schema import parse_schema

_SHA = re.compile(r"^[0-9a-f]{64}$")


def _missing(field: str, where: str) -> AuditError:
    return AuditError("MANIFEST_MISSING_FIELD", f"{where} 缺少字段 {field}", where)


def _req_str(obj: dict[str, Any], field: str, where: str) -> str:
    if field not in obj:
        raise _missing(field, where)
    value = obj[field]
    if not isinstance(value, str) or not value:
        raise AuditError("MANIFEST_TYPE_ERROR", f"{where}.{field} 必须是非空字符串", where)
    return value


def _req_int(obj: dict[str, Any], field: str, where: str, minimum: int = 0) -> int:
    if field not in obj:
        raise _missing(field, where)
    value = obj[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AuditError(
            "MANIFEST_TYPE_ERROR", f"{where}.{field} 必须是 >= {minimum} 的整数", where
        )
    return value


def _sha(value: Any, field: str, where: str, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _SHA.match(value):
        raise AuditError("MANIFEST_TYPE_ERROR", f"{where}.{field} 必须是 sha256", where)
    return value


def parse_table(raw: Any, source: str = "table.json") -> TableDef:
    if not isinstance(raw, dict):
        raise AuditError("MANIFEST_TYPE_ERROR", "table.json 顶层必须是对象", source)
    if "schema" not in raw:
        raise _missing("schema", source)
    table_id = _req_str(raw, "table_id", source)
    table_name = _req_str(raw, "table_name", source)
    rule_version = _req_str(raw, "rule_version", source)
    created_at = _req_str(raw, "created_at", source)
    expected_visible = _req_str(raw, "expected_visible", source)
    if "format_version" in raw and (
        isinstance(raw["format_version"], bool) or raw["format_version"] != 1
    ):
        raise AuditError("MANIFEST_TYPE_ERROR", "仅支持 format_version=1", source)
    schema = parse_schema(raw["schema"], source)
    return TableDef(
        table_id, table_name, rule_version, created_at, expected_visible, schema
    )


def _parse_data(item: Any, source: str, index: int) -> DataFileSpec:
    where = f"{source}#data_files[{index}]"
    if not isinstance(item, dict):
        raise AuditError("MANIFEST_TYPE_ERROR", f"{where} 必须是对象", where)
    file_id = _req_str(item, "file_id", where)
    path = _req_str(item, "path", where)
    fmt = _req_str(item, "format", where).lower()
    if fmt not in ("csv", "jsonl"):
        raise AuditError("UNKNOWN_FORMAT", f"未知数据格式: {fmt}", where)
    row_count = _req_int(item, "row_count", where)
    seq = _req_int(item, "sequence_number", where)
    sha = _sha(item.get("sha256"), "sha256", where)
    schema_hash = _req_str(item, "schema_hash", where)
    return DataFileSpec(file_id, path, fmt, row_count, sha or "", seq, schema_hash)


def _parse_delete(item: Any, source: str, index: int) -> DeleteFileSpec:
    where = f"{source}#delete_files[{index}]"
    if not isinstance(item, dict):
        raise AuditError("MANIFEST_TYPE_ERROR", f"{where} 必须是对象", where)
    file_id = _req_str(item, "file_id", where)
    path = _req_str(item, "path", where)
    kind = _req_str(item, "delete_kind", where).lower()
    if kind not in ("position", "equality"):
        raise AuditError("UNKNOWN_DELETE_KIND", f"未知删除类型: {kind}", where)
    seq = _req_int(item, "sequence_number", where)
    schema_hash = _req_str(item, "schema_hash", where)
    sha = _sha(item.get("sha256"), "sha256", where, required=False)
    target = item.get("target_file_id")
    if target is not None and (not isinstance(target, str) or not target):
        raise AuditError("MANIFEST_TYPE_ERROR", "target_file_id 必须是非空字符串", where)
    columns = item.get("equality_columns", [])
    if not isinstance(columns, list) or any(
        not isinstance(c, str) or not c for c in columns
    ):
        raise AuditError("MANIFEST_TYPE_ERROR", "equality_columns 必须是字符串数组", where)
    return DeleteFileSpec(
        file_id, path, kind, seq, schema_hash, sha, target, tuple(columns)
    )


def parse_snapshot(
    raw: Any, source: str, schema_hash: str, findings: list[Finding]
) -> Snapshot:
    if not isinstance(raw, dict):
        raise AuditError("MANIFEST_TYPE_ERROR", "快照顶层必须是对象", source)
    snapshot_id = _req_str(raw, "snapshot_id", source)
    sequence = _req_int(raw, "sequence_number", source)
    declared_hash = _req_str(raw, "schema_hash", source)
    created_at = _req_str(raw, "created_at", source)
    data_raw = raw.get("data_files")
    delete_raw = raw.get("delete_files", [])
    if not isinstance(data_raw, list):
        raise AuditError("MANIFEST_TYPE_ERROR", "snapshot.data_files 必须是数组", source)
    if not isinstance(delete_raw, list):
        raise AuditError("MANIFEST_TYPE_ERROR", "snapshot.delete_files 必须是数组", source)
    if declared_hash != schema_hash:
        findings.append(
            finding(
                "SCHEMA_HASH_MISMATCH",
                "快照模式哈希与表模式不一致",
                source,
                None,
                {"declared": declared_hash, "expected": schema_hash},
            )
        )
    data_files: list[DataFileSpec] = []
    delete_files: list[DeleteFileSpec] = []
    seen_data: set[str] = set()
    seen_delete: set[str] = set()
    for index, item in enumerate(data_raw):
        try:
            spec = _parse_data(item, source, index)
        except AuditError as exc:
            findings.append(finding(exc.code, exc.message, source, None, {"index": index}))
            continue
        if spec.file_id in seen_data:
            findings.append(
                finding("DUPLICATE_ID", "重复数据文件 ID", source, None, {"file_id": spec.file_id})
            )
            continue
        seen_data.add(spec.file_id)
        data_files.append(spec)
    for index, item in enumerate(delete_raw):
        try:
            spec = _parse_delete(item, source, index)
        except AuditError as exc:
            findings.append(finding(exc.code, exc.message, source, None, {"index": index}))
            continue
        if spec.file_id in seen_delete:
            findings.append(
                finding("DUPLICATE_ID", "重复删除文件 ID", source, None, {"file_id": spec.file_id})
            )
            continue
        seen_delete.add(spec.file_id)
        delete_files.append(spec)
    return Snapshot(
        snapshot_id,
        sequence,
        declared_hash,
        created_at,
        tuple(data_files),
        tuple(delete_files),
        source,
    )
