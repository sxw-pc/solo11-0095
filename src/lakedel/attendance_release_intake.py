"""Deletion-aware synthetic attendance disclosure intake.

naive_preview 保留为反面对照：它只按 mask_below 隐藏小格却发布全部行列
合计，敏感格可被行列合计精确反推。main 现在签发
lakedel.attendance_disclosure.build_release 的合同化最小隐藏披露报告。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .attendance_disclosure import build_release
from .engine import execute_audit
from .util import pretty_json


def naive_preview(data: dict, result) -> dict:
    if result.workspace.snapshot.snapshot_id != data["snapshot_id"] or result.run_key != data["run_key"]:
        raise ValueError("attendance release is bound to a different audit")
    if result.stats["diffs"] != 0:
        raise ValueError("unreconciled visible rows cannot be released")
    rows = data["rows"]
    columns = data["columns"]
    counts = {row: {column: 0 for column in columns} for row in rows}
    visible = {decision.key for decision in result.decisions if decision.visible}
    for record in result.workspace.rows:
        if (record.file_id, record.row_pos) in visible:
            counts[record.values["category"]][record.values["region"]] += 1
    threshold = data["mask_below"]
    return {
        "snapshot_id": data["snapshot_id"], "run_key": result.run_key,
        "visible_cells": {f"{row}:{column}": counts[row][column] for row in rows for column in columns
                          if counts[row][column] >= threshold},
        "masked_cells": [f"{row}:{column}" for row in rows for column in columns
                         if counts[row][column] < threshold],
        "row_totals": {row: sum(counts[row].values()) for row in rows},
        "column_totals": {column: sum(counts[row][column] for row in rows) for column in columns},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue deletion-aware synthetic attendance disclosure")
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args(argv)
    data = json.loads(args.fixture.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[2]
    result = execute_audit(root / "examples/attendance_release/lake")
    print(pretty_json(build_release(data, result)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
