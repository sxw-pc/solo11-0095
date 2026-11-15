"""Merge-on-Read 可见性与逐行决定链。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .models import DataFileSpec, DeleteOperation, RowDecision, RowRecord


def _matches_equality(row: RowRecord, operation: DeleteOperation) -> bool:
    if operation.key is None:
        return False
    return all(row.values.get(name) == value for name, value in operation.key.items())


def _chain(
    visible: bool,
    reason: str,
    position_hits: list[DeleteOperation],
    equality_hits: list[DeleteOperation],
    stale_hits: list[DeleteOperation],
) -> tuple[dict[str, Any], ...]:
    steps: list[dict[str, Any]] = [
        {"step": "initial_visibility", "visible": True, "detail": "行默认可见"}
    ]
    for op in position_hits:
        steps.append(
            {
                "step": "position_delete",
                "source": op.delete_file_id,
                "target": {"file_id": op.target_file_id, "row_pos": op.row_pos},
                "visible": False,
                "detail": "命中位置删除",
            }
        )
    for op in equality_hits:
        steps.append(
            {
                "step": "equality_delete",
                "source": op.delete_file_id,
                "key": op.key,
                "visible": False,
                "detail": "命中等值删除且数据序列号不晚于删除序列号",
            }
        )
    for op in stale_hits:
        steps.append(
            {
                "step": "stale_delete_ignored",
                "source": op.delete_file_id,
                "sequence_number": op.sequence_number,
                "visible": True,
                "detail": "删除序列号早于数据文件序列号，不应用",
            }
        )
    steps.append({"step": "final_visibility", "visible": visible, "reason_code": reason})
    return tuple(steps)


def compute_visibility(
    rows: tuple[RowRecord, ...],
    data_specs: tuple[DataFileSpec, ...],
    operations: tuple[DeleteOperation, ...],
) -> tuple[tuple[RowDecision, ...], dict[str, int]]:
    sequences = {item.file_id: item.sequence_number for item in data_specs}
    position_ops = [
        op for op in operations if op.delete_kind == "position"
    ]
    equality_ops = [
        op for op in operations if op.delete_kind == "equality"
    ]
    decisions: list[RowDecision] = []
    for row in rows:
        data_sequence = sequences.get(row.file_id, 1 << 30)
        position_hits = sorted(
            [
                op for op in position_ops
                if op.applicable
                and op.target_file_id == row.file_id
                and op.row_pos == row.row_pos
            ],
            key=lambda op: (op.delete_file_id, op.ordinal),
        )
        equality_hits = sorted(
            [
                op for op in equality_ops
                if op.applicable
                and data_sequence <= op.sequence_number
                and (not op.target_file_id or op.target_file_id == row.file_id)
                and _matches_equality(row, op)
            ],
            key=lambda op: (op.delete_file_id, op.ordinal),
        )
        stale_hits = sorted(
            [
                op for op in position_ops
                if op.target_file_id == row.file_id
                and op.row_pos == row.row_pos
                and data_sequence > op.sequence_number
            ],
            key=lambda op: (op.delete_file_id, op.ordinal),
        )
        visible = not position_hits and not equality_hits
        if position_hits and equality_hits:
            reason = "DELETED_BY_POSITION_AND_EQUALITY"
        elif position_hits:
            reason = "DELETED_BY_POSITION"
        elif equality_hits:
            reason = "DELETED_BY_EQUALITY"
        elif stale_hits:
            reason = "VISIBLE_STALE_DELETE_IGNORED"
        else:
            reason = "VISIBLE_NO_DELETE"
        decisions.append(
            RowDecision(
                row.file_id,
                row.row_pos,
                row.row_id,
                visible,
                reason,
                tuple(op.delete_file_id for op in position_hits),
                tuple(op.delete_file_id for op in equality_hits),
                tuple(op.delete_file_id for op in stale_hits),
                _chain(visible, reason, position_hits, equality_hits, stale_hits),
            )
        )
    counts = Counter(item.reason_code for item in decisions)
    return tuple(decisions), dict(sorted(counts.items()))


def visibility_by_file(decisions: tuple[RowDecision, ...]) -> list[dict[str, int | str]]:
    stats: dict[str, list[int]] = {}
    for decision in decisions:
        bucket = stats.setdefault(decision.file_id, [0, 0])
        bucket[0] += 1
        bucket[1] += 1 if decision.visible else 0
    return [
        {
            "file_id": file_id,
            "rows": total,
            "visible": visible,
            "hidden": total - visible,
        }
        for file_id, (total, visible) in sorted(stats.items())
    ]
