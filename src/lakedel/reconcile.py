"""期望可见结果读取与逐行对账。"""

from __future__ import annotations

import csv
from pathlib import Path

from .errors import Finding, finding
from .models import (
    DiffRecord,
    ExpectedRow,
    ReconciliationOutcome,
    RowDecision,
    RowRecord,
)

DIFF_CODES = (
    "MISSING_VISIBLE",
    "UNEXPECTED_VISIBLE",
    "EXTRA_EXPECTED_ROW",
    "MISSING_EXPECTED_ROW",
)
_TRUE = {"true", "1", "yes", "y", "visible"}
_FALSE = {"false", "0", "no", "n", "hidden"}


def _parse_visible(value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return None


def load_expected(path: Path, findings: list[Finding]) -> tuple[ExpectedRow, ...]:
    try:
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            records = list(csv.reader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        findings.append(
            finding("EXPECTED_INVALID", f"期望文件读取失败: {exc}", str(path))
        )
        return ()
    header_index = next((i for i, record in enumerate(records) if record), None)
    if header_index is None:
        findings.append(finding("EXPECTED_INVALID", "期望文件为空", str(path)))
        return ()
    for line_no in range(header_index):
        findings.append(finding("BLANK_LINE", "期望文件表头前空行", str(path), line_no + 1))
    header = [cell.strip() for cell in records[header_index]]
    required = {"file_id", "row_pos", "expected_visible"}
    missing = sorted(required - set(header))
    if missing:
        findings.append(
            finding("MISSING_COLUMN", "期望文件缺少列", str(path), None,
                    {"columns": missing})
        )
        return ()
    result: list[ExpectedRow] = []
    seen: set[tuple[str, int]] = set()
    for line_no, record in enumerate(records[header_index + 1 :], start=header_index + 2):
        if not record or all(cell.strip() == "" for cell in record):
            findings.append(finding("BLANK_LINE", "期望文件空行", str(path), line_no))
            continue
        raw = dict(zip(header, record))
        file_id = raw.get("file_id", "").strip()
        try:
            row_pos = int(raw.get("row_pos", "").strip())
        except ValueError:
            row_pos = -1
        visible = _parse_visible(raw.get("expected_visible", ""))
        if not file_id or row_pos < 0 or visible is None:
            findings.append(
                finding("EXPECTED_INVALID", "期望行字段非法", str(path), line_no,
                        {"file_id": file_id, "row_pos": row_pos,
                         "expected_visible": raw.get("expected_visible")})
            )
            continue
        key = (file_id, row_pos)
        if key in seen:
            findings.append(
                finding("DUPLICATE_EXPECTED_ROW", "期望行重复", str(path), line_no,
                        {"file_id": file_id, "row_pos": row_pos})
            )
            continue
        seen.add(key)
        row_id = raw.get("_row_id") or None
        result.append(ExpectedRow(file_id, row_pos, row_id, visible))
    result.sort(key=lambda item: (item.file_id, item.row_pos))
    return tuple(result)


def _first_related(
    file_id: str, row_pos: int, findings: list[Finding]
) -> str:
    exact = sorted(
        (f for f in findings if f.file == file_id and f.row == row_pos),
        key=lambda f: (f.code, f.message),
    )
    if exact:
        return exact[0].code
    file_level = sorted(
        (f for f in findings if f.file == file_id and f.row is None),
        key=lambda f: (f.code, f.message),
    )
    return file_level[0].code if file_level else ""


def reconcile(
    rows: tuple[RowRecord, ...],
    decisions: tuple[RowDecision, ...],
    expected: tuple[ExpectedRow, ...],
    findings: list[Finding],
) -> tuple[tuple[DiffRecord, ...], dict[str, int]]:
    """按暂定布尔结果对账，保留原有四分类语义。"""
    expected_map = {(item.file_id, item.row_pos): item for item in expected}
    data_map = {(item.file_id, item.row_pos): item for item in rows}
    decision_map = {item.key: item for item in decisions}
    diffs: list[DiffRecord] = []
    for row in rows:
        key = (row.file_id, row.row_pos)
        decision = decision_map[key]
        expected_row = expected_map.get(key)
        classification = ""
        if expected_row is None:
            classification = "MISSING_EXPECTED_ROW"
        elif decision.visible != expected_row.visible:
            classification = (
                "UNEXPECTED_VISIBLE" if decision.visible else "MISSING_VISIBLE"
            )
        if not classification:
            continue
        if decision.indeterminate:
            classification_certainty = "indeterminate"
        else:
            classification_certainty = decision.certainty
        related = _first_related(row.file_id, row.row_pos, findings)
        diff = DiffRecord(
            classification,
            row.file_id,
            row.row_pos,
            row.row_id,
            decision.visible,
            None if expected_row is None else expected_row.visible,
            related,
            decision.chain_summary,
        )
        diffs.append(diff)
        findings.append(
            finding(
                classification,
                "实际可见性与期望不一致",
                row.file_id,
                row.row_pos,
                {
                    "actual_visible": decision.visible,
                    "expected_visible": diff.expected_visible,
                    "reason_code": decision.reason_code,
                    "certainty": classification_certainty,
                    "trusted_hits": list(decision.trusted_hits),
                    "suspect_hits": list(decision.suspect_hits),
                },
            )
        )
    for item in expected:
        if (item.file_id, item.row_pos) in data_map:
            continue
        diffs.append(
            DiffRecord(
                "EXTRA_EXPECTED_ROW",
                item.file_id,
                item.row_pos,
                item.row_id,
                None,
                item.visible,
                _first_related(item.file_id, item.row_pos, findings),
                "预期存在但数据中无此行",
            )
        )
        findings.append(
            finding(
                "EXTRA_EXPECTED_ROW",
                "期望文件包含数据中不存在的行",
                item.file_id,
                item.row_pos,
                {"expected_visible": item.visible},
            )
        )
    diffs.sort(key=lambda item: (item.file_id, item.row_pos, item.classification))
    summary = {code: 0 for code in DIFF_CODES}
    for item in diffs:
        summary[item.classification] += 1
    summary["total"] = len(diffs)
    return tuple(diffs), summary


def reconcile_certainty(
    rows: tuple[RowRecord, ...],
    decisions: tuple[RowDecision, ...],
    expected: tuple[ExpectedRow, ...],
    diffs: tuple[DiffRecord, ...],
    data_trusted: bool,
    delete_trusted: bool,
) -> ReconciliationOutcome:
    """发布核验：只有全部 expected 行的确定态与期望一致且无待核定才可发布。

    调查视图不受此限制，仍可展示暂定布尔结果，但必须标注可信区间与来源。
    """
    expected_map = {(item.file_id, item.row_pos): item for item in expected}
    decision_map = {item.key: item for item in decisions}
    data_map = {(item.file_id, item.row_pos): item for item in rows}
    blockers: list[str] = []
    indeterminate_rows: list[tuple[str, int]] = []
    mismatches: list[tuple[str, int]] = []
    if not data_trusted:
        blockers.append("UNTRUSTED_DATA_FILE")
    if not delete_trusted:
        blockers.append("UNTRUSTED_DELETE_FILE")
    for decision in decisions:
        if not decision.indeterminate:
            continue
        indeterminate_rows.append(decision.key)
        if not decision.row_identity_trusted:
            code = "INDETERMINATE_DATA_SOURCE"
        else:
            code = "INDETERMINATE_DELETE_SOURCE"
        if code not in blockers:
            blockers.append(code)
    for expected_row in expected:
        key = (expected_row.file_id, expected_row.row_pos)
        decision = decision_map.get(key)
        if key not in data_map or decision is None:
            mismatches.append(key)
            continue
        if decision.indeterminate:
            continue
        if decision.certainty == "certain_visible" and expected_row.visible:
            continue
        if decision.certainty == "certain_hidden" and not expected_row.visible:
            continue
        mismatches.append(key)
    for diff in diffs:
        key = (diff.file_id, diff.row_pos)
        if diff.classification in ("EXTRA_EXPECTED_ROW", "MISSING_EXPECTED_ROW"):
            if key not in mismatches:
                mismatches.append(key)
            continue
        decision = decision_map.get(key)
        if decision is None or decision.indeterminate:
            continue
        if key not in mismatches:
            mismatches.append(key)
    certain_visible = sum(1 for item in decisions if item.certain_visible)
    certain_hidden = sum(1 for item in decisions if item.certain_hidden)
    indeterminate = sum(1 for item in decisions if item.indeterminate)
    provisional_visible = sum(1 for item in decisions if item.visible)
    publishable = not blockers and not indeterminate_rows and not mismatches
    return ReconciliationOutcome(
        publishable,
        tuple(blockers),
        tuple(sorted(set(indeterminate_rows))),
        tuple(sorted(set(mismatches))),
        provisional_visible,
        len(decisions) - provisional_visible,
        certain_visible,
        certain_hidden,
        indeterminate,
    )
