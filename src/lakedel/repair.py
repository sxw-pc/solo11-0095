"""可见性差异的反事实修复预演。

只输出候选处置计划（暂停删除条目 / 隔离整份删除文件 / 补偿精确位置删除），
不修改任何数据文件、删除文件、期望文件或既有报告。计划是否可行完全由
内存中的删除适用 → 可见性 → 对账重跑决定，而不是在既有 diff 上删行。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .engine import execute_audit
from .errors import AuditError
from .models import AuditResult, DeleteOperation
from .reconcile import reconcile
from .util import (
    FIXED_RUN_AT,
    atomic_write_text,
    canonical_json,
    pretty_json,
    sha256_text,
)
from .visibility import compute_visibility, equality_hit, position_hit

SCOPES = ("entry", "file")
COMPENSATE_FILE_ID = "plan_compensate"
PLAN_FILES = {"json": "repair_plan.json", "md": "repair_plan.md"}

LIMITATIONS = [
    "计划只是反事实预演：不修改任何数据文件、删除文件、期望文件或既有报告。",
    "只有 applicable 且无冲突的删除条目被视为可撤证据；无效、过期、重复、孤儿与模式不符的条目只列入排除清单。",
    "补偿删除是建议性的精确位置删除：目标文件必须存在、位置必须在声明行数内、序列号不早于目标数据文件。",
    "未列入期望文件的行不允许改变可见性；期望中引用但数据里不存在的行无法通过删除处置补回。",
]


def _row_ref(row: Any) -> dict[str, Any]:
    return {"file_id": row.file_id, "row_pos": row.row_pos, "row_id": row.row_id}


def build_evidence_graph(
    rows: tuple[Any, ...],
    data_specs: tuple[Any, ...],
    operations: tuple[DeleteOperation, ...],
) -> dict[str, Any]:
    """条目 → 命中行 的证据图；不可信条目只进排除清单。"""
    sequences = {item.file_id: item.sequence_number for item in data_specs}
    trustworthy: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    ordered = sorted(operations, key=lambda op: (op.delete_file_id, op.ordinal))
    for op in ordered:
        if not op.applicable or op.conflict_code:
            excluded.append(
                {
                    "delete_file_id": op.delete_file_id,
                    "ordinal": op.ordinal,
                    "delete_kind": op.delete_kind,
                    "conflict_code": op.conflict_code or "NOT_APPLICABLE",
                }
            )
            continue
        hits: list[dict[str, Any]] = []
        for row in rows:
            data_sequence = sequences.get(row.file_id, 1 << 30)
            if op.delete_kind == "position":
                matched = position_hit(row, op)
            else:
                matched = equality_hit(row, op, data_sequence)
            if matched:
                hits.append(_row_ref(row))
        trustworthy.append(
            {
                "delete_file_id": op.delete_file_id,
                "ordinal": op.ordinal,
                "delete_kind": op.delete_kind,
                "sequence_number": op.sequence_number,
                "target_file_id": op.target_file_id,
                "row_pos": op.row_pos,
                "key": op.key,
                "reason": op.reason,
                "hits": hits,
            }
        )
    return {"trustworthy_entries": trustworthy, "excluded_entries": excluded}


def _rehearse(
    result: AuditResult,
    suspended: frozenset[tuple[str, int]],
    compensations: tuple[DeleteOperation, ...],
) -> tuple[Any, ...]:
    """内存中重跑删除适用 → 可见性 → 对账，返回 (decisions, diffs, summary, reason_counts)。"""
    data = result.workspace
    operations: list[DeleteOperation] = []
    for op in result.operations:
        if (op.delete_file_id, op.ordinal) in suspended:
            operations.append(replace(op, applicable=False))
        else:
            operations.append(op)
    operations.extend(compensations)
    decisions, reason_counts = compute_visibility(
        data.rows, data.snapshot.data_files, tuple(operations)
    )
    findings = list(result.findings)
    diffs, diff_summary = reconcile(data.rows, decisions, data.expected, findings)
    return decisions, diffs, diff_summary, reason_counts


def _compensation_op(
    file_id: str,
    row_pos: int,
    sequences: dict[str, int],
    snapshot_sequence: int,
    ordinal: int,
) -> DeleteOperation:
    sequence = max(snapshot_sequence, sequences[file_id])
    return DeleteOperation(
        COMPENSATE_FILE_ID,
        ordinal,
        "position",
        sequence,
        file_id,
        row_pos,
        None,
        "plan-repair 补偿：建议新增精确位置删除",
        (),
        True,
        "",
    )


def _evaluate(
    result: AuditResult,
    expected_map: dict[tuple[str, int], Any],
    baseline_visible: dict[tuple[str, int], bool],
    decisions: tuple[Any, ...],
    entry_hits: dict[tuple[str, int], list[dict[str, Any]]],
    suspended: frozenset[tuple[str, int]],
) -> list[dict[str, Any]]:
    """可行性判定：有效期望行全部达标，且未列入期望的行可见性不变。"""
    decision_map = {item.key: item for item in decisions}
    row_ids = {
        (row.file_id, row.row_pos): row.row_id for row in result.workspace.rows
    }
    conflicts: list[dict[str, Any]] = []
    for diff in result.diffs:
        if diff.classification != "EXTRA_EXPECTED_ROW":
            continue
        conflicts.append(
            {
                "code": "EXPECTED_ROW_NOT_IN_DATA",
                "message": "期望行在数据中不存在，删除处置无法补回该行",
                "evidence": {
                    "file_id": diff.file_id,
                    "row_pos": diff.row_pos,
                    "row_id": diff.row_id,
                    "expected_visible": diff.expected_visible,
                },
            }
        )
    for key in sorted(expected_map):
        decision = decision_map.get(key)
        if decision is None:
            continue
        expected = expected_map[key]
        if decision.visible != expected.visible:
            conflicts.append(
                {
                    "code": "RESIDUAL_EXPECTED_MISMATCH",
                    "message": "预演之后期望行仍未达到预期可见性",
                    "evidence": {
                        "file_id": key[0],
                        "row_pos": key[1],
                        "row_id": row_ids.get(key),
                        "expected_visible": expected.visible,
                        "actual_visible": decision.visible,
                        "reason_code": decision.reason_code,
                    },
                }
            )
    for key in sorted(baseline_visible):
        if key in expected_map:
            continue
        decision = decision_map.get(key)
        if decision is None or decision.visible == baseline_visible[key]:
            continue
        revealed_by = []
        for ident in sorted(suspended):
            hits = entry_hits.get(ident, [])
            if any(
                hit["file_id"] == key[0] and hit["row_pos"] == key[1] for hit in hits
            ):
                revealed_by.append(
                    {"delete_file_id": ident[0], "ordinal": ident[1]}
                )
        conflicts.append(
            {
                "code": "UNTRACKED_ROW_VISIBILITY_CHANGED",
                "message": "未列入期望的行可见性被处置动作改变",
                "evidence": {
                    "file_id": key[0],
                    "row_pos": key[1],
                    "row_id": row_ids.get(key),
                    "before_visible": baseline_visible[key],
                    "after_visible": decision.visible,
                    "revealed_by": revealed_by,
                },
            }
        )
    return conflicts


def _row_effect(
    key: tuple[str, int],
    row_ids: dict[tuple[str, int], Any],
    expected_map: dict[tuple[str, int], Any],
    baseline: dict[tuple[str, int], Any],
    final: dict[tuple[str, int], Any],
) -> dict[str, Any]:
    before = baseline[key]
    after = final[key]
    expected = expected_map.get(key)
    return {
        "file_id": key[0],
        "row_pos": key[1],
        "row_id": row_ids.get(key),
        "expected_visible": None if expected is None else expected.visible,
        "before_visible": before.visible,
        "after_visible": after.visible,
        "before_reason_code": before.reason_code,
        "after_reason_code": after.reason_code,
    }


def plan_repair(
    workspace: str | Path,
    snapshot_id: str | None = None,
    scope: str = "entry",
    run_at: str = FIXED_RUN_AT,
) -> dict[str, Any]:
    """生成候选处置计划；只读 workspace，全部预演在内存中完成。"""
    if scope not in SCOPES:
        raise AuditError("CLI_USAGE", f"--scope 仅支持 {'|'.join(SCOPES)}")
    result = execute_audit(workspace, snapshot_id, run_at)
    data = result.workspace
    rows = data.rows
    expected_map = {(item.file_id, item.row_pos): item for item in data.expected}
    baseline = {item.key: item for item in result.decisions}
    baseline_visible = {key: item.visible for key, item in baseline.items()}
    row_ids = {(row.file_id, row.row_pos): row.row_id for row in rows}
    sequences = {item.file_id: item.sequence_number for item in data.snapshot.data_files}
    graph = build_evidence_graph(rows, data.snapshot.data_files, result.operations)
    entry_by_id = {
        (entry["delete_file_id"], entry["ordinal"]): entry
        for entry in graph["trustworthy_entries"]
    }
    entry_hits = {
        ident: entry["hits"] for ident, entry in entry_by_id.items()
    }
    hits_by_row: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for ident, entry in entry_by_id.items():
        for hit in entry["hits"]:
            hits_by_row.setdefault((hit["file_id"], hit["row_pos"]), []).append(ident)

    # 1. 期望可见但实际不可见的行：其全部可信命中都必须撤下（重叠证据需同时暂停）。
    suspend: set[tuple[str, int]] = set()
    for diff in result.diffs:
        if diff.classification != "MISSING_VISIBLE":
            continue
        suspend.update(hits_by_row.get((diff.file_id, diff.row_pos), ()))
    quarantined: set[str] = set()
    if scope == "file":
        quarantined = {ident[0] for ident in suspend}
        suspend = {
            (op.delete_file_id, op.ordinal)
            for op in result.operations
            if op.delete_file_id in quarantined
        }
    suspended = frozenset(suspend)

    # 2. 期望隐藏但实际可见的行：逐一提出补偿精确位置删除。
    compensate: dict[tuple[str, int], str] = {}
    for diff in result.diffs:
        if diff.classification == "UNEXPECTED_VISIBLE":
            compensate[(diff.file_id, diff.row_pos)] = "UNEXPECTED_VISIBLE"

    def _comp_ops(mapping: dict[tuple[str, int], str]) -> tuple[DeleteOperation, ...]:
        return tuple(
            _compensation_op(
                key[0], key[1], sequences, data.snapshot.sequence_number, index
            )
            for index, key in enumerate(sorted(mapping), start=1)
        )

    # 3. 第一次预演：找出被暂停动作波及、但期望隐藏的行，追加补偿。
    decisions_1, _, _, _ = _rehearse(result, suspended, _comp_ops(compensate))
    decision_map_1 = {item.key: item for item in decisions_1}
    for row in rows:
        key = (row.file_id, row.row_pos)
        expected = expected_map.get(key)
        if expected is None or expected.visible:
            continue
        if decision_map_1[key].visible and key not in compensate:
            compensate[key] = "REVEALED_BY_SUSPENSION"

    # 4. 最终预演与可行性判定。
    compensations = _comp_ops(compensate)
    decisions_f, diffs_f, summary_f, reason_counts_f = _rehearse(
        result, suspended, compensations
    )
    final = {item.key: item for item in decisions_f}
    conflicts = _evaluate(
        result, expected_map, baseline_visible, decisions_f, entry_hits, suspended
    )
    feasible = not conflicts

    # 5. 最小性验证：可行时逐一移除动作（entry 为单条目，file 为整份文件），
    #    预演必须不再可行；补偿删除同样逐一验证必要性。
    minimal = True
    if feasible and (suspended or compensate):
        if scope == "entry":
            groups = [frozenset({ident}) for ident in sorted(suspended)]
        else:
            groups = [
                frozenset(ident for ident in suspended if ident[0] == file_id)
                for file_id in sorted(quarantined)
            ]
        for group in groups:
            trial = frozenset(suspended - group)
            decisions_t, _, _, _ = _rehearse(result, trial, compensations)
            if not _evaluate(
                result, expected_map, baseline_visible, decisions_t, entry_hits, trial
            ):
                minimal = False
                break
        if minimal:
            for index in range(len(compensations)):
                trial_comp = tuple(
                    op for i, op in enumerate(compensations) if i != index
                )
                decisions_t, _, _, _ = _rehearse(result, suspended, trial_comp)
                if not _evaluate(
                    result, expected_map, baseline_visible, decisions_t, entry_hits,
                    suspended,
                ):
                    minimal = False
                    break

    # 6. 组装动作、行级影响与计划文档。
    actions: list[dict[str, Any]] = []
    if scope == "entry":
        for ident in sorted(suspended):
            entry = entry_by_id[ident]
            hits = entry["hits"]
            actions.append(
                {
                    "action_id": f"suspend:{ident[0]}#{ident[1]}",
                    "kind": "suspend_entry",
                    "delete_file_id": ident[0],
                    "ordinal": ident[1],
                    "evidence": {
                        "delete_kind": entry["delete_kind"],
                        "sequence_number": entry["sequence_number"],
                        "target_file_id": entry["target_file_id"],
                        "row_pos": entry["row_pos"],
                        "key": entry["key"],
                        "reason": entry["reason"],
                        "hits": hits,
                    },
                    "affected_rows": [
                        _row_effect(
                            (hit["file_id"], hit["row_pos"]),
                            row_ids, expected_map, baseline, final,
                        )
                        for hit in hits
                    ],
                }
            )
    else:
        for file_id in sorted(quarantined):
            entries = [
                entry
                for entry in graph["trustworthy_entries"]
                if entry["delete_file_id"] == file_id
            ]
            excluded = [
                entry
                for entry in graph["excluded_entries"]
                if entry["delete_file_id"] == file_id
            ]
            hits = [hit for entry in entries for hit in entry["hits"]]
            actions.append(
                {
                    "action_id": f"quarantine:{file_id}",
                    "kind": "quarantine_file",
                    "delete_file_id": file_id,
                    "entries": [entry["ordinal"] for entry in entries]
                    + [entry["ordinal"] for entry in excluded],
                    "evidence": {
                        "trustworthy_entries": [
                            {
                                "ordinal": entry["ordinal"],
                                "delete_kind": entry["delete_kind"],
                                "sequence_number": entry["sequence_number"],
                                "hits": entry["hits"],
                            }
                            for entry in entries
                        ],
                        "excluded_entries": [
                            {
                                "ordinal": entry["ordinal"],
                                "conflict_code": entry["conflict_code"],
                            }
                            for entry in excluded
                        ],
                    },
                    "affected_rows": [
                        _row_effect(
                            (hit["file_id"], hit["row_pos"]),
                            row_ids, expected_map, baseline, final,
                        )
                        for hit in hits
                    ],
                }
            )
    comp_sequence = {
        (op.target_file_id, op.row_pos): op.sequence_number for op in compensations
    }
    for key in sorted(compensate):
        actions.append(
            {
                "action_id": f"compensate:{key[0]}#{key[1]}",
                "kind": "compensate_position",
                "target": {"file_id": key[0], "row_pos": key[1]},
                "evidence": {
                    "trigger": compensate[key],
                    "expected_visible": False,
                    "row_id": row_ids.get(key),
                },
                "proposed_delete": {
                    "delete_file_id": COMPENSATE_FILE_ID,
                    "delete_kind": "position",
                    "target_file_id": key[0],
                    "row_pos": key[1],
                    "sequence_number": comp_sequence[key],
                    "schema_hash": data.table.schema.schema_hash,
                    "checks": [
                        "target_file_exists",
                        "row_pos_in_range",
                        "sequence_not_stale",
                        "schema_hash_matches",
                    ],
                },
                "affected_rows": [
                    _row_effect(key, row_ids, expected_map, baseline, final)
                ],
            }
        )

    row_effects = [
        _row_effect(key, row_ids, expected_map, baseline, final)
        for key in sorted(baseline)
        if baseline[key].visible != final[key].visible
        or baseline[key].reason_code != final[key].reason_code
    ]
    residual = [item.to_dict() for item in diffs_f]
    plan_key = sha256_text(
        canonical_json(
            {
                "scope": scope,
                "feasible": feasible,
                "suspended": sorted(f"{f}#{o}" for f, o in suspended),
                "quarantined": sorted(quarantined),
                "compensations": sorted(f"{f}#{p}" for f, p in compensate),
                "snapshot_id": data.snapshot.snapshot_id,
                "workspace_fingerprint": data.fingerprint,
                "baseline_diffs": result.stats["diffs"],
                "residual_diffs": summary_f["total"],
                "conflicts": [item["code"] for item in conflicts],
            }
        )
    )
    primary = len(suspended) if scope == "entry" else len(quarantined)
    summary = {
        "scope": scope,
        "feasible": feasible,
        "baseline_diffs": result.stats["diffs"],
        "suspended_entries": len(suspended),
        "quarantined_files": len(quarantined),
        "compensation_entries": len(compensate),
        "actions": len(actions),
        "changed_rows": len(row_effects),
        "residual_diffs": summary_f["total"],
        "conflicts": len(conflicts),
        "minimal": minimal,
        "minimality": {
            "primary": primary,
            "primary_metric": (
                "suspended_entries" if scope == "entry" else "quarantined_files"
            ),
            "secondary": len(compensate),
            "secondary_metric": "compensation_entries",
            "tie_break": "delete_file_id,ordinal",
        },
        "plan_key": plan_key,
    }
    return {
        "project": {
            "name": "行删镜",
            "package": "lakedel",
            "version": __version__,
            "rule_version": result.rule_version,
        },
        "run": {
            "run_at": result.run_at,
            "workspace_fingerprint": data.fingerprint,
            "snapshot_id": data.snapshot.snapshot_id,
            "scope": scope,
        },
        "feasible": feasible,
        "baseline": {
            "diffs": result.stats["diffs"],
            "diff_summary": dict(result.diff_summary),
        },
        "evidence_graph": graph,
        "actions": actions,
        "row_effects": row_effects,
        "rehearsal": {
            "diffs": summary_f["total"],
            "diff_summary": dict(summary_f),
            "reason_counts": dict(reason_counts_f),
            "residual_diffs": residual,
        },
        "conflicts": conflicts,
        "summary": summary,
        "limitations": list(LIMITATIONS),
    }


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_plan_markdown(plan: dict[str, Any]) -> str:
    """字节稳定的可读摘要；可行与不可行共用同一模板。"""
    summary = plan["summary"]
    if summary["feasible"]:
        conclusion = (
            f"预演可行：{summary['actions']} 个处置动作使所有有效期望行达到预期可见性，"
            "未列入期望的行可见性不变。本计划仅为预演，未修改任何源数据。"
        )
    else:
        conclusion = (
            f"预演不可行：{summary['conflicts']} 个冲突使计划无法满足全部期望；"
            "以下候选动作与冲突证据仅供复核，不是可执行方案。"
        )
    lines = [
        "# 行删镜修复预演计划",
        "",
        "## 结论",
        "",
        conclusion,
        "",
        f"plan_key：`{summary['plan_key']}`",
        "",
        "## 统计",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
    ]
    for key in (
        "scope", "feasible", "baseline_diffs", "suspended_entries",
        "quarantined_files", "compensation_entries", "actions", "changed_rows",
        "residual_diffs", "conflicts", "minimal",
    ):
        lines.append(f"| {key} | {summary[key]} |")
    lines.extend(["", "## 证据图", ""])
    trustworthy = plan["evidence_graph"]["trustworthy_entries"]
    excluded = plan["evidence_graph"]["excluded_entries"]
    lines.append(f"可信条目 {len(trustworthy)} 条，排除条目 {len(excluded)} 条。")
    lines.extend(
        [
            "",
            "| delete_file_id | ordinal | 类型 | 命中行数 |",
            "| --- | ---: | --- | ---: |",
        ]
    )
    for entry in trustworthy:
        lines.append(
            f"| {_cell(entry['delete_file_id'])} | {entry['ordinal']} | "
            f"{_cell(entry['delete_kind'])} | {len(entry['hits'])} |"
        )
    lines.extend(
        [
            "",
            "| delete_file_id | ordinal | 排除原因 |",
            "| --- | ---: | --- |",
        ]
    )
    if excluded:
        for entry in excluded:
            lines.append(
                f"| {_cell(entry['delete_file_id'])} | {entry['ordinal']} | "
                f"{_cell(entry['conflict_code'])} |"
            )
    else:
        lines.append("| 无 | - | - |")
    lines.extend(
        [
            "",
            "## 处置动作",
            "",
            "| action_id | 类型 | 受影响行数 |",
            "| --- | --- | ---: |",
        ]
    )
    if plan["actions"]:
        for action in plan["actions"]:
            lines.append(
                f"| {_cell(action['action_id'])} | {_cell(action['kind'])} | "
                f"{len(action['affected_rows'])} |"
            )
    else:
        lines.append("| 无 | - | 0 |")
    lines.extend(
        [
            "",
            "| 动作 | 行 | 预演前 reason_code | 预演后 reason_code |",
            "| --- | --- | --- | --- |",
        ]
    )
    row_lines = 0
    for action in plan["actions"]:
        for effect in action["affected_rows"]:
            row_lines += 1
            lines.append(
                f"| {_cell(action['action_id'])} | "
                f"{_cell(effect['file_id'])}:{effect['row_pos']} | "
                f"{_cell(effect['before_reason_code'])} | "
                f"{_cell(effect['after_reason_code'])} |"
            )
    if not row_lines:
        lines.append("| 无 | - | - | - |")
    compensations = [
        action for action in plan["actions"] if action["kind"] == "compensate_position"
    ]
    lines.extend(
        [
            "",
            "## 补偿删除",
            "",
            "| 目标行 | 触发 | 建议序列号 |",
            "| --- | --- | ---: |",
        ]
    )
    if compensations:
        for action in compensations:
            target = action["target"]
            lines.append(
                f"| {_cell(target['file_id'])}:{target['row_pos']} | "
                f"{_cell(action['evidence']['trigger'])} | "
                f"{action['proposed_delete']['sequence_number']} |"
            )
    else:
        lines.append("| 无 | - | - |")
    lines.extend(["", "## 冲突", ""])
    if plan["conflicts"]:
        lines.extend(["| code | 说明 | 证据 |", "| --- | --- | --- |"])
        for conflict in plan["conflicts"]:
            lines.append(
                f"| {_cell(conflict['code'])} | {_cell(conflict['message'])} | "
                f"{_cell(canonical_json(conflict['evidence']))} |"
            )
    else:
        lines.append("无冲突。")
    lines.extend(["", "## 残余差异", ""])
    residual = plan["rehearsal"]["residual_diffs"]
    if residual:
        lines.extend(
            [
                "| 分类 | file_id | row_pos | 实际 | 期望 |",
                "| --- | --- | ---: | --- | --- |",
            ]
        )
        for item in residual:
            lines.append(
                f"| {_cell(item['classification'])} | {_cell(item['file_id'])} | "
                f"{item['row_pos']} | {_cell(item['actual_visible'])} | "
                f"{_cell(item['expected_visible'])} |"
            )
    else:
        lines.append("无残余差异。")
    lines.extend(["", "## 限制说明", ""])
    lines.extend(f"- {item}" for item in plan["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_plan(out_dir: str | Path, plan: dict[str, Any]) -> list[dict[str, Any]]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []
    for fmt in ("json", "md"):
        text = pretty_json(plan) if fmt == "json" else render_plan_markdown(plan)
        path = out / PLAN_FILES[fmt]
        atomic_write_text(path, text)
        artifacts.append({"path": path, "format": fmt, "sha256": sha256_text(text)})
    return artifacts


def load_plan_reference(path: str | Path, result: AuditResult) -> dict[str, Any]:
    """读取计划摘要供报告/台账引用；上下文不一致时拒绝。"""
    source = str(path)
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuditError("REPAIR_PLAN_INVALID", f"修复计划读取失败: {exc}", source) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("run"), dict) \
            or not isinstance(payload.get("summary"), dict):
        raise AuditError("REPAIR_PLAN_INVALID", "修复计划缺少 run/summary 结构", source)
    run = payload["run"]
    summary = payload["summary"]
    mismatches = []
    if run.get("workspace_fingerprint") != result.workspace.fingerprint:
        mismatches.append("workspace_fingerprint")
    if run.get("snapshot_id") != result.workspace.snapshot.snapshot_id:
        mismatches.append("snapshot_id")
    if payload.get("project", {}).get("rule_version") != result.rule_version:
        mismatches.append("rule_version")
    if mismatches:
        raise AuditError(
            "REPAIR_PLAN_MISMATCH",
            "修复计划与当前审计上下文不一致: " + ",".join(sorted(mismatches)),
            source,
        )
    return {
        "plan_key": summary.get("plan_key", ""),
        "scope": summary.get("scope", ""),
        "feasible": bool(summary.get("feasible")),
        "actions": summary.get("actions", 0),
        "compensations": summary.get("compensation_entries", 0),
        "residual_diffs": summary.get("residual_diffs", 0),
        "source": source,
    }
