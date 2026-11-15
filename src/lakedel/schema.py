"""表模式解析、稳定哈希与最小类型校验。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .errors import AuditError
from .models import Column, TableSchema
from .util import canonical_json, sha256_text

ALLOWED_TYPES = ("string", "long", "date")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LONG = re.compile(r"^-?[0-9]+$")


def _column_payload(columns: tuple[Column, ...]) -> list[dict[str, Any]]:
    return [
        {"name": c.name, "type": c.dtype, "nullable": c.nullable}
        for c in columns
    ]


def schema_hash(columns: tuple[Column, ...]) -> str:
    return sha256_text(canonical_json(_column_payload(columns)))


def parse_schema(raw: Any, source: str = "table.json") -> TableSchema:
    if not isinstance(raw, dict):
        raise AuditError("MANIFEST_TYPE_ERROR", "schema 必须是对象", source)
    if "columns" not in raw:
        raise AuditError("MANIFEST_MISSING_FIELD", "schema.columns 缺失", source)
    items = raw["columns"]
    if not isinstance(items, list) or not items:
        raise AuditError("SCHEMA_INVALID", "schema.columns 必须是非空数组", source)
    columns: list[Column] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise AuditError("SCHEMA_INVALID", f"第 {index} 列不是对象", source)
        for key in ("name", "type", "nullable"):
            if key not in item:
                raise AuditError("MANIFEST_MISSING_FIELD", f"列缺少字段 {key}", source)
        name, dtype, nullable = item["name"], item["type"], item["nullable"]
        if not isinstance(name, str) or not _NAME.match(name):
            raise AuditError("SCHEMA_INVALID", f"非法列名: {name!r}", source)
        if name in seen:
            raise AuditError("DUPLICATE_COLUMN", f"重复列名: {name}", source)
        if dtype not in ALLOWED_TYPES:
            raise AuditError("SCHEMA_INVALID", f"不支持的列类型: {dtype!r}", source)
        if not isinstance(nullable, bool):
            raise AuditError("MANIFEST_TYPE_ERROR", f"{name}.nullable 必须是布尔值", source)
        seen.add(name)
        columns.append(Column(name, dtype, nullable, index))
    pos_column = next((c for c in columns if c.name == "_row_pos"), None)
    if pos_column is None or pos_column.dtype != "long" or pos_column.nullable:
        raise AuditError(
            "SCHEMA_INVALID", "schema 必须包含非空 long 类型 _row_pos", source
        )
    result = tuple(columns)
    return TableSchema(result, schema_hash(result))


def coerce_value(column: Column, raw: Any) -> tuple[bool, Any, str]:
    """返回 (是否合法, 规范化值, 错误说明)。"""
    if raw is None or (isinstance(raw, str) and raw == ""):
        if column.nullable:
            return True, None, ""
        return False, None, "非空列不允许空值"
    if column.dtype == "string":
        if isinstance(raw, str):
            return True, raw, ""
        return False, None, "期望 string"
    if column.dtype == "long":
        if isinstance(raw, bool):
            return False, None, "布尔值不是 long"
        if isinstance(raw, int):
            return True, raw, ""
        if isinstance(raw, str) and _LONG.match(raw):
            try:
                return True, int(raw), ""
            except ValueError:
                pass
        return False, None, "期望 long"
    if column.dtype == "date":
        if not isinstance(raw, str):
            return False, None, "期望 date 字符串"
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return False, None, "期望 YYYY-MM-DD"
        if parsed.strftime("%Y-%m-%d") != raw:
            return False, None, "日期格式不规范"
        return True, raw, ""
    return False, None, "未知类型"
