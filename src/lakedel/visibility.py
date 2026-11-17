"""Merge-on-Read 可见性与逐行完整性证明链。

三态语义：

- ``certain_hidden``：数据行身份可信，且至少一个**可信**位置/等值删除有效命中。
- ``certain_visible``：数据行身份可信，没有任何可信命中，且所有可能适用的
  可信与不可信删除都被排除。不可信删除的键值不可确认，因此只要它在清单
  声明的作用域内仍可能命中该行，就不能给出确定可见。
- ``indeterminate``：其余情况。暂定布尔结果仍按当前可解析内容计算，供调查
  视图展示，但不能支持发布核验。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .models import (
    DataFileSpec,
    DeleteFileSpec,
    DeleteOperation,
    RowDecision,
    RowRecord,
    SourceIntegrity,
)

CERTAIN_VISIBLE = "certain_visible"
CERTAIN_HIDDEN = "certain_hidden"
INDETERMINATE = "indeterminate"


def _matches_equality(row: RowRecord, operation: DeleteOperation) -> bool:
    if operation.key is None:
        return False
    return all(row.values.get(name) == value for name, value in operation.key.items())


def _trusted_interval(decision: RowDecision) -> list[bool]:
    if decision.indeterminate:
        return [False, True]
    return [decision.certain_visible, decision.certain_visible]


def _untrusted_scope(
    untrusted_specs: list[DeleteFileSpec],
    untrusted_ops: list[DeleteOperation],
    row: RowRecord,
    data_sequence: int,
) -> list[tuple[str, str]]:
    """受损删除在清单声明作用域内是否仍可能影响该行。

    文件哈希不符时，其内容（键值或坐标）都可能被替换，因此不能把解析出的
    条目当作事实。若整行无法解析，仍要按清单声明的目标与序列号计算保守
    作用域；否则受损文件会被错误地忽略，从而把不确定行误判为确定可见。
    """
    if untrusted_specs:
        scope = [
            (spec.file_id, spec.delete_kind)
            for spec in untrusted_specs
            if data_sequence <= spec.sequence_number
            and (not spec.target_file_id or spec.target_file_id == row.file_id)
        ]
        return sorted(set(scope))
    # 仅拿到操作时（例如内部单元测试）退化为逐条目作用域。
    scope = []
    for op in untrusted_ops:
        if not op.applicable or op.source_trusted:
            continue
        if data_sequence > op.sequence_number:
            continue
        if op.target_file_id and op.target_file_id != row.file_id:
            continue
        scope.append((op.delete_file_id, op.delete_kind))
    return sorted(set(scope))


def _chain(
    visible: bool,
    reason: str,
    position_hits: list[DeleteOperation],
    equality_hits: list[DeleteOperation],
    stale_hits: list[DeleteOperation],
    suspect_position_hits: list[DeleteOperation],
    suspect_equality_hits: list[DeleteOperation],
    untrusted_scope: list[tuple[str, str]],
    row: RowRecord,
    certainty: str,
) -> tuple[dict[str, Any], ...]:
    steps: list[dict[str, Any]] = [
        {"step": "initial_visibility", "visible": True, "detail": "行默认可见"}
    ]
    if not row.source_trusted:
        steps.append(
            {
                "step": "data_source_untrusted",
                "source": row.file_id,
                "declared_sha256": row.source_declared_sha256,
                "actual_sha256": row.source_actual_sha256,
                "trusted": False,
                "detail": "数据文件哈希不符，行身份与等值键不可作为确定证据",
            }
        )
    for op in position_hits:
        steps.append(
            {
                "step": "position_delete",
                "source": op.delete_file_id,
                "target": {"file_id": op.target_file_id, "row_pos": op.row_pos},
                "visible": False,
                "trusted": True,
                "detail": "命中可信位置删除",
            }
        )
    for op in suspect_position_hits:
        steps.append(
            {
                "step": "suspect_position_delete",
                "source": op.delete_file_id,
                "target": {"file_id": op.target_file_id, "row_pos": op.row_pos},
                "visible": False,
                "trusted": False,
                "detail": "命中受损位置删除，仅作调查证据，不单独支持确定结论",
            }
        )
    for op in equality_hits:
        steps.append(
            {
                "step": "equality_delete",
                "source": op.delete_file_id,
                "key": op.key,
                "visible": False,
                "trusted": True,
                "detail": "命中可信等值删除且数据序列号不晚于删除序列号",
            }
        )
    for op in suspect_equality_hits:
        steps.append(
            {
                "step": "suspect_equality_delete",
                "source": op.delete_file_id,
                "key": op.key,
                "visible": False,
                "trusted": False,
                "detail": "受损等值删除按当前解析内容命中，仅作调查证据",
            }
        )
    if untrusted_scope:
        steps.append(
            {
                "step": "untrusted_delete_scope",
                "sources": sorted({file_id for file_id, _ in untrusted_scope}),
                "trusted": False,
                "detail": "受损删除在声明作用域内仍可能影响此行，无法排除",
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
    steps.append(
        {
            "step": "final_visibility",
            "visible": visible,
            "reason_code": reason,
            "certainty": certainty,
        }
    )
    return tuple(steps)


def compute_visibility(
    rows: tuple[RowRecord, ...],
    data_specs: tuple[DataFileSpec, ...],
    operations: tuple[DeleteOperation, ...],
    delete_specs: tuple[DeleteFileSpec, ...] = (),
    delete_integrity: tuple[SourceIntegrity, ...] = (),
) -> tuple[tuple[RowDecision, ...], dict[str, int]]:
    sequences = {item.file_id: item.sequence_number for item in data_specs}
    position_ops = [op for op in operations if op.delete_kind == "position"]
    equality_ops = [op for op in operations if op.delete_kind == "equality"]
    untrusted_ids = {
        item.file_id for item in delete_integrity if item.mismatch
    }
    untrusted_specs = [
        spec for spec in delete_specs if spec.file_id in untrusted_ids
    ]
    untrusted_ops = [op for op in operations if not op.source_trusted]

    # 同一受损等值删除文件命中的行集合，作为跨行共享的来源证据。
    # 一个文件可含多个键，必须合并而不是只取首个条目的匹配。
    untrusted_equality_rows: dict[str, set[tuple[str, int]]] = {}
    for op in equality_ops:
        if op.source_trusted or not op.applicable or op.key is None:
            continue
        matched = {
            (row.file_id, row.row_pos)
            for row in rows
            if (op.target_file_id is None or op.target_file_id == row.file_id)
            and sequences.get(row.file_id, 1 << 30) <= op.sequence_number
            and _matches_equality(row, op)
        }
        untrusted_equality_rows.setdefault(op.delete_file_id, set()).update(matched)

    decisions: list[RowDecision] = []
    for row in rows:
        data_sequence = sequences.get(row.file_id, 1 << 30)
        position_hits = sorted(
            [
                op
                for op in position_ops
                if op.applicable
                and op.target_file_id == row.file_id
                and op.row_pos == row.row_pos
            ],
            key=lambda op: (op.delete_file_id, op.ordinal),
        )
        equality_hits = sorted(
            [
                op
                for op in equality_ops
                if op.applicable
                and data_sequence <= op.sequence_number
                and (not op.target_file_id or op.target_file_id == row.file_id)
                and _matches_equality(row, op)
            ],
            key=lambda op: (op.delete_file_id, op.ordinal),
        )
        stale_hits = sorted(
            [
                op
                for op in position_ops
                if op.target_file_id == row.file_id
                and op.row_pos == row.row_pos
                and data_sequence > op.sequence_number
            ],
            key=lambda op: (op.delete_file_id, op.ordinal),
        )
        trusted_position_hits = [op for op in position_hits if op.source_trusted]
        trusted_equality_hits = [op for op in equality_hits if op.source_trusted]
        suspect_position_hits = [op for op in position_hits if not op.source_trusted]
        suspect_equality_hits = [op for op in equality_hits if not op.source_trusted]
        untrusted_scope = _untrusted_scope(
            untrusted_specs, untrusted_ops, row, data_sequence
        )
        row_untrusted_ids = sorted({file_id for file_id, _ in untrusted_scope})
        untrusted_equality_ids = sorted(
            {
                file_id
                for file_id, kind in untrusted_scope
                if kind == "equality"
            }
        )
        shared_rows: set[tuple[str, int]] = set()
        for file_id in untrusted_equality_ids:
            shared_rows.update(untrusted_equality_rows.get(file_id, set()))

        trusted_hit = bool(trusted_position_hits or trusted_equality_hits)
        row_trusted = row.source_trusted
        certain_hidden = row_trusted and trusted_hit
        certain_visible = row_trusted and not trusted_hit and not untrusted_scope
        indeterminate = not (certain_hidden or certain_visible)
        provisional_visible = not position_hits and not equality_hits

        if indeterminate:
            reason = (
                "INDETERMINATE_UNTRUSTED_DATA"
                if not row_trusted
                else "INDETERMINATE_UNTRUSTED_DELETE"
            )
        elif trusted_position_hits and trusted_equality_hits:
            reason = "DELETED_BY_POSITION_AND_EQUALITY"
        elif trusted_position_hits:
            reason = "DELETED_BY_POSITION"
        elif trusted_equality_hits:
            reason = "DELETED_BY_EQUALITY"
        elif stale_hits:
            reason = "VISIBLE_STALE_DELETE_IGNORED"
        else:
            reason = "VISIBLE_NO_DELETE"

        certainty = (
            INDETERMINATE
            if indeterminate
            else (CERTAIN_HIDDEN if certain_hidden else CERTAIN_VISIBLE)
        )
        decisions.append(
            RowDecision(
                row.file_id,
                row.row_pos,
                row.row_id,
                provisional_visible,
                reason,
                tuple(op.delete_file_id for op in position_hits),
                tuple(op.delete_file_id for op in equality_hits),
                tuple(op.delete_file_id for op in stale_hits),
                _chain(
                    provisional_visible,
                    reason,
                    trusted_position_hits,
                    trusted_equality_hits,
                    stale_hits,
                    suspect_position_hits,
                    suspect_equality_hits,
                    untrusted_scope,
                    row,
                    certainty,
                ),
                certainty,
                certain_visible,
                certain_hidden,
                indeterminate,
                tuple(op.delete_file_id for op in trusted_position_hits),
                tuple(op.delete_file_id for op in trusted_equality_hits),
                tuple(op.delete_file_id for op in suspect_position_hits),
                tuple(op.delete_file_id for op in suspect_equality_hits),
                tuple(untrusted_equality_ids),
                tuple(sorted(shared_rows)),
                row_trusted,
                tuple(row_untrusted_ids),
            )
        )
    counts = Counter(item.reason_code for item in decisions)
    return tuple(decisions), dict(sorted(counts.items()))


def certainty_counts(decisions: tuple[RowDecision, ...]) -> dict[str, int]:
    counts = {
        CERTAIN_VISIBLE: 0,
        CERTAIN_HIDDEN: 0,
        INDETERMINATE: 0,
        "rows": len(decisions),
        "provisional_visible": 0,
        "provisional_hidden": 0,
    }
    for decision in decisions:
        counts[decision.certainty] += 1
        counts["provisional_visible" if decision.visible else "provisional_hidden"] += 1
    return counts


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


def certainty_by_file(decisions: tuple[RowDecision, ...]) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, int]] = {}
    for decision in decisions:
        bucket = stats.setdefault(
            decision.file_id,
            {
                "rows": 0,
                "certain_visible": 0,
                "certain_hidden": 0,
                "indeterminate": 0,
                "provisional_visible": 0,
                "provisional_hidden": 0,
            },
        )
        bucket["rows"] += 1
        bucket[decision.certainty] += 1
        bucket["provisional_visible" if decision.visible else "provisional_hidden"] += 1
    return [
        {"file_id": file_id, **values} for file_id, values in sorted(stats.items())
    ]


def trusted_interval(decision: RowDecision) -> list[bool]:
    """该行的可信可见性区间：确定态为单点，待核定为 [false, true]。"""
    return _trusted_interval(decision)
