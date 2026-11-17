"""数据文件读取、行身份与行级校验。"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .errors import AuditError, Finding, finding
from .models import (
    Column,
    DataFileSpec,
    RowRecord,
    SourceIntegrity,
    TableSchema,
)
from .schema import coerce_value
from .util import require_file, safe_join, sha256_file


def data_integrity(
    root: Path, specs: tuple[DataFileSpec, ...]
) -> tuple[SourceIntegrity, ...]:
    """按清单声明校验数据文件字节，不修复、不改写任何输入。"""
    result: list[SourceIntegrity] = []
    for spec in sorted(specs, key=lambda item: item.file_id):
        path = require_file(safe_join(root, spec.path), spec.path)
        actual = sha256_file(path)
        mismatch = actual != spec.sha256
        result.append(
            SourceIntegrity(
                "data",
                spec.file_id,
                spec.path,
                spec.sha256,
                actual,
                not mismatch,
                mismatch,
            )
        )
    return tuple(result)


def _header_check(
    spec: DataFileSpec,
    header: list[str],
    schema: TableSchema,
    findings: list[Finding],
) -> set[str]:
    allowed = set(schema.names) | {"_row_id"}
    required = set(schema.names) | {"_row_pos"}
    seen: set[str] = set()
    for name in header:
        if name in seen:
            findings.append(
                finding("DUPLICATE_COLUMN", "数据文件表头重复", spec.path, None, {"column": name})
            )
        seen.add(name)
    for name in sorted(required - set(header)):
        findings.append(
            finding("MISSING_COLUMN", "数据文件缺少列", spec.path, None, {"column": name})
        )
    for name in sorted(set(header) - allowed):
        findings.append(
            finding("EXTRA_COLUMN", "数据文件包含未知列", spec.path, None, {"column": name})
        )
    return set(header)


def _build_row(
    spec: DataFileSpec,
    raw: dict[str, Any],
    present: set[str],
    schema: TableSchema,
    line: int,
    findings: list[Finding],
    check_missing: bool,
    integrity: SourceIntegrity,
) -> RowRecord | None:
    pos_column = schema.column("_row_pos")
    assert pos_column is not None
    ok, row_pos, message = coerce_value(pos_column, raw.get("_row_pos"))
    if not ok or not isinstance(row_pos, int) or row_pos < 0:
        findings.append(
            finding(
                "INVALID_VALUE",
                "行定位 _row_pos 非法",
                spec.path,
                line,
                {"message": message, "value": raw.get("_row_pos")},
            )
        )
        return None
    values: dict[str, Any] = {"_row_pos": row_pos}
    for column in schema.columns:
        if column.name == "_row_pos":
            continue
        if column.name not in present:
            if check_missing:
                findings.append(
                    finding(
                        "MISSING_COLUMN",
                        "数据行缺少字段",
                        spec.path,
                        line,
                        {"column": column.name},
                    )
                )
            continue
        good, value, detail = coerce_value(column, raw.get(column.name))
        if not good:
            findings.append(
                finding(
                    "INVALID_VALUE",
                    "字段值不符合模式类型",
                    spec.path,
                    row_pos,
                    {"column": column.name, "message": detail, "value": raw.get(column.name)},
                )
            )
            value = None
        values[column.name] = value
    row_id_value = raw.get("_row_id")
    if "_row_id" in schema.names:
        row_id = values.get("_row_id")
        if row_id is not None and not isinstance(row_id, str):
            row_id = None
    elif row_id_value is None or isinstance(row_id_value, str):
        row_id = row_id_value
    else:
        findings.append(
            finding("INVALID_VALUE", "_row_id 必须是字符串", spec.path, row_pos, {})
        )
        row_id = None
    return RowRecord(
        spec.file_id,
        row_pos,
        row_id,
        values,
        spec.path,
        integrity.trusted,
        integrity.actual_sha256,
        integrity.declared_sha256,
    )


def _read_csv(
    spec: DataFileSpec,
    path: Path,
    schema: TableSchema,
    findings: list[Finding],
    integrity: SourceIntegrity,
) -> list[RowRecord]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            records = list(csv.reader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        findings.append(finding("FILE_DECODE_ERROR", f"CSV 读取失败: {exc}", spec.path))
        return []
    header_index = next((i for i, row in enumerate(records) if row), None)
    if header_index is None:
        findings.append(finding("MISSING_COLUMN", "CSV 没有表头", spec.path))
        return []
    for line in range(header_index):
        findings.append(finding("BLANK_LINE", "表头前存在空行", spec.path, line + 1))
    header = [cell.strip() for cell in records[header_index]]
    if header:
        header[0] = header[0].lstrip("﻿")
    present = _header_check(spec, header, schema, findings)
    rows: list[RowRecord] = []
    seen: set[int] = set()
    count = 0
    for offset, record in enumerate(records[header_index + 1 :], start=header_index + 2):
        if not record or all(cell.strip() == "" for cell in record):
            findings.append(finding("BLANK_LINE", "CSV 空行", spec.path, offset))
            continue
        count += 1
        if len(record) > len(header):
            findings.append(
                finding(
                    "INVALID_ROW",
                    "CSV 行单元格多于表头",
                    spec.path,
                    offset,
                    {"cells": len(record), "columns": len(header)},
                )
            )
        raw = dict(zip(header, record))
        row = _build_row(spec, raw, present, schema, offset, findings, False, integrity)
        if row is None:
            continue
        if row.row_pos in seen:
            findings.append(
                finding("DUPLICATE_ROW_POSITION", "重复 row_pos", spec.path, row.row_pos)
            )
            continue
        seen.add(row.row_pos)
        rows.append(row)
    if count != spec.row_count:
        findings.append(
            finding(
                "ROW_COUNT_MISMATCH",
                "数据文件行数与清单不一致",
                spec.path,
                None,
                {"declared": spec.row_count, "actual": count},
            )
        )
    return rows


def _read_jsonl(
    spec: DataFileSpec,
    path: Path,
    schema: TableSchema,
    findings: list[Finding],
    integrity: SourceIntegrity,
) -> list[RowRecord]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        findings.append(finding("FILE_DECODE_ERROR", f"JSONL 读取失败: {exc}", spec.path))
        return []
    rows: list[RowRecord] = []
    seen: set[int] = set()
    count = 0
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            findings.append(finding("BLANK_LINE", "JSONL 空行", spec.path, line_no))
            continue
        count += 1
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            findings.append(
                finding("INVALID_JSON_LINE", f"JSONL 解析失败: {exc.msg}", spec.path, line_no)
            )
            continue
        if not isinstance(raw, dict):
            findings.append(finding("INVALID_ROW", "JSONL 行必须是对象", spec.path, line_no))
            continue
        allowed = set(schema.names) | {"_row_id"}
        present = set(raw)
        for name in sorted(present - allowed):
            findings.append(
                finding("EXTRA_COLUMN", "JSONL 行包含未知字段", spec.path, line_no, {"column": name})
            )
        row = _build_row(spec, raw, present, schema, line_no, findings, True, integrity)
        if row is None:
            continue
        if row.row_pos in seen:
            findings.append(
                finding("DUPLICATE_ROW_POSITION", "重复 row_pos", spec.path, row.row_pos)
            )
            continue
        seen.add(row.row_pos)
        rows.append(row)
    if count != spec.row_count:
        findings.append(
            finding(
                "ROW_COUNT_MISMATCH",
                "数据文件行数与清单不一致",
                spec.path,
                None,
                {"declared": spec.row_count, "actual": count},
            )
        )
    return rows


def read_data_files(
    root: Path,
    specs: tuple[DataFileSpec, ...],
    schema: TableSchema,
    integrity: tuple[SourceIntegrity, ...] | None = None,
) -> tuple[tuple[RowRecord, ...], list[Finding]]:
    """读取数据文件；完整性状态随行传递，供证明链区分可信与可疑证据。"""
    findings: list[Finding] = []
    rows: list[RowRecord] = []
    seen_ids: dict[str, tuple[str, int]] = {}
    integrity_by_file = {
        item.file_id: item
        for item in (integrity if integrity is not None else data_integrity(root, specs))
    }
    for spec in sorted(specs, key=lambda item: item.file_id):
        path = require_file(safe_join(root, spec.path), spec.path)
        state = integrity_by_file[spec.file_id]
        if state.mismatch:
            findings.append(
                finding(
                    "FILE_HASH_MISMATCH",
                    "数据文件 sha256 与清单不一致",
                    spec.path,
                    None,
                    {
                        "declared": spec.sha256,
                        "actual": state.actual_sha256,
                        "trusted": False,
                        "impact": "row_identity_untrusted",
                    },
                )
            )
        if spec.format == "csv":
            file_rows = _read_csv(spec, path, schema, findings, state)
        else:
            file_rows = _read_jsonl(spec, path, schema, findings, state)
        for row in file_rows:
            if row.row_id is not None:
                if row.row_id in seen_ids:
                    findings.append(
                        finding(
                            "DUPLICATE_ROW_ID",
                            "业务行 ID 重复",
                            row.source,
                            row.row_pos,
                            {"row_id": row.row_id, "first": list(seen_ids[row.row_id])},
                        )
                    )
                else:
                    seen_ids[row.row_id] = (row.file_id, row.row_pos)
            rows.append(row)
    rows.sort(key=lambda row: (row.file_id, row.row_pos))
    return tuple(rows), findings
