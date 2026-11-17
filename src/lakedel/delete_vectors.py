"""删除文件加载、文件级检查与冲突编排。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .delete_rules import (
    SpecState,
    equality_operation,
    matching_rows,
    position_operation,
)
from .errors import Finding, finding
from .models import (
    DataFileSpec,
    DeleteFileSpec,
    DeleteOperation,
    RowRecord,
    SourceIntegrity,
    TableSchema,
)
from .util import canonical_json, require_file, safe_join, sha256_file


def _read_entries(
    spec: DeleteFileSpec, path: Path, findings: list[Finding]
) -> list[tuple[int, dict[str, Any]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        findings.append(finding("FILE_DECODE_ERROR", f"删除文件读取失败: {exc}", spec.path))
        return []
    entries: list[tuple[int, dict[str, Any]]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            findings.append(finding("BLANK_LINE", "删除文件空行", spec.path, line_no))
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            findings.append(
                finding("INVALID_JSON_LINE", f"删除行 JSON 非法: {exc.msg}", spec.path, line_no)
            )
            continue
        if not isinstance(entry, dict):
            findings.append(
                finding("DELETE_ENTRY_INVALID", "删除条目必须是对象", spec.path, line_no)
            )
            continue
        entries.append((line_no, entry))
    return entries


def _spec_integrity(
    root: Path,
    spec: DeleteFileSpec,
    schema: TableSchema,
    data_by_id: dict[str, DataFileSpec],
    findings: list[Finding],
) -> tuple[Path, SpecState, SourceIntegrity]:
    path = require_file(safe_join(root, spec.path), spec.path)
    actual = sha256_file(path)
    mismatch = spec.sha256 is not None and actual != spec.sha256
    if mismatch:
        findings.append(
            finding(
                "FILE_HASH_MISMATCH",
                "删除文件 sha256 与清单不一致",
                spec.path,
                None,
                {
                    "declared": spec.sha256,
                    "actual": actual,
                    "trusted": False,
                    "impact": "delete_evidence_untrusted",
                },
            )
        )
    integrity = SourceIntegrity(
        "delete",
        spec.file_id,
        spec.path,
        spec.sha256,
        actual,
        not mismatch,
        mismatch,
    )
    schema_ok = spec.schema_hash == schema.schema_hash
    if not schema_ok:
        findings.append(
            finding("DELETE_SCHEMA_MISMATCH", "删除文件模式哈希与表模式不一致", spec.path,
                    None, {"declared": spec.schema_hash, "expected": schema.schema_hash})
        )
    columns_ok = True
    if spec.delete_kind == "equality":
        cols = spec.equality_columns
        if not cols or len(set(cols)) != len(cols) or any(
            schema.column(name) is None for name in cols
        ):
            columns_ok = False
            findings.append(
                finding("AMBIGUOUS_EQUALITY_COLUMNS",
                        "等值删除列缺失、重复或不在模式中", spec.path, None,
                        {"equality_columns": list(cols)})
            )
    target_ok = True
    if spec.target_file_id and spec.target_file_id not in data_by_id:
        target_ok = False
        findings.append(
            finding("ORPHAN_DELETE_TARGET", "删除文件声明的目标数据文件不存在",
                    spec.path, None, {"target_file_id": spec.target_file_id})
        )
    stale = False
    target_spec = data_by_id.get(spec.target_file_id or "")
    if target_spec is not None:
        stale = spec.sequence_number < target_spec.sequence_number
        if stale:
            findings.append(
                finding("STALE_SEQUENCE", "删除文件序列号早于目标数据文件", spec.path,
                        None, {"delete_sequence": spec.sequence_number,
                               "data_sequence": target_spec.sequence_number})
            )
    return path, SpecState(schema_ok, columns_ok, target_ok, stale), integrity


def load_delete_vectors(
    root: Path,
    specs: tuple[DeleteFileSpec, ...],
    schema: TableSchema,
    data_specs: tuple[DataFileSpec, ...],
    rows: tuple[RowRecord, ...],
    integrity_out: list[SourceIntegrity] | None = None,
) -> tuple[tuple[DeleteOperation, ...], list[Finding]]:
    findings: list[Finding] = []
    operations: list[DeleteOperation] = []
    data_by_id = {item.file_id: item for item in data_specs}
    sequences = {item.file_id: item.sequence_number for item in data_specs}
    seen_positions: set[tuple[str, int]] = set()
    for spec in sorted(specs, key=lambda item: item.file_id):
        path, state, integrity = _spec_integrity(
            root, spec, schema, data_by_id, findings
        )
        if integrity_out is not None:
            integrity_out.append(integrity)
        seen_equality: set[str] = set()
        for line_no, entry in _read_entries(spec, path, findings):
            reason = entry.get("reason", "")
            if not isinstance(reason, str):
                reason = ""
            if spec.delete_kind == "position":
                op = position_operation(
                    spec, entry, line_no, reason, state, data_by_id,
                    seen_positions, findings,
                )
                if op is not None:
                    operations.append(
                        replace(
                            op,
                            source_trusted=integrity.trusted,
                            source_actual_sha256=integrity.actual_sha256,
                            source_declared_sha256=integrity.declared_sha256,
                        )
                    )
                continue
            op, normalized = equality_operation(
                spec, entry, line_no, reason, schema, state, findings
            )
            if op is None:
                continue
            op = replace(
                op,
                source_trusted=integrity.trusted,
                source_actual_sha256=integrity.actual_sha256,
                source_declared_sha256=integrity.declared_sha256,
            )
            if normalized is not None and not op.conflict_code:
                marker = canonical_json(normalized)
                if marker in seen_equality:
                    findings.append(
                        finding("DUPLICATE_EQUALITY_DELETE",
                                "同一删除文件内重复等值键", spec.path, line_no,
                                {"key": normalized})
                    )
                    op = replace(
                        op, applicable=False, conflict_code="DUPLICATE_EQUALITY_DELETE"
                    )
                seen_equality.add(marker)
            if not op.conflict_code and op.key is not None:
                if not matching_rows(op.key, spec, rows, sequences):
                    findings.append(
                        finding("EQUALITY_NO_MATCH", "等值删除没有得到任何匹配行",
                                spec.path, line_no, {"key": op.key})
                    )
                    op = replace(
                        op, applicable=False, conflict_code="EQUALITY_NO_MATCH"
                    )
            operations.append(op)
    return tuple(operations), findings
