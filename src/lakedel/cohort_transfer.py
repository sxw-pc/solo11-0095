"""Bind a pre-deletion inspection draw to two verified lake snapshots."""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from .engine import execute_audit
from .util import canonical_json, sha256_text


def _visible_rows(audit):
    if audit.diffs or any(item.severity == "error" for item in audit.findings):
        raise ValueError("snapshot audit is not clean")
    rows = {
        row.row_id: row
        for row, decision in zip(audit.workspace.rows, audit.decisions)
        if decision.visible and row.row_id is not None
    }
    if len(rows) != audit.stats["visible_rows"]:
        raise ValueError("visible row identity is missing or duplicated")
    return rows


def inspect_deletion_cohort(root: str | Path, draw_path: str | Path) -> dict:
    """Report the verified frames and observations; leave release inference open."""
    root = Path(root)
    draw = json.loads(Path(draw_path).read_text(encoding="utf-8"))
    if draw.get("design") != "srswor_by_region_before_deletion":
        raise ValueError("unsupported pre-deletion draw design")
    before = execute_audit(root / "before")
    after = execute_audit(root / "after")
    if (draw.get("before_snapshot"), draw.get("after_snapshot")) != (
        before.workspace.snapshot.snapshot_id, after.workspace.snapshot.snapshot_id
    ):
        raise ValueError("draw snapshot binding does not match audits")
    before_rows = _visible_rows(before)
    after_rows = _visible_rows(after)
    if not set(after_rows) < set(before_rows):
        raise ValueError("after frame must be a strict subset of before frame")
    if any(after_rows[row_id].values != before_rows[row_id].values for row_id in after_rows):
        raise ValueError("surviving row content changed across snapshots")
    if set(row.row_id for row in before.workspace.rows) != set(row.row_id for row in after.workspace.rows):
        raise ValueError("underlying row identities changed across snapshots")

    seed = draw.get("seed")
    sizes = draw.get("sample_size_by_region")
    inspections = draw.get("inspections")
    if type(seed) is not int or not isinstance(sizes, dict) or not isinstance(inspections, list):
        raise ValueError("draw seed, sizes or inspections invalid")
    by_region: dict[str, list[str]] = {}
    for row_id, row in before_rows.items():
        by_region.setdefault(row.values["region"], []).append(row_id)
    if set(sizes) != set(by_region):
        raise ValueError("draw regions do not match before frame")
    rng = random.Random(seed)
    selected = set()
    for region, row_ids in sorted(by_region.items()):
        count = sizes[region]
        if type(count) is not int or not 0 < count <= len(row_ids):
            raise ValueError("invalid region sample size")
        selected.update(rng.sample(sorted(row_ids), count))
    seen = set()
    observed = []
    for item in inspections:
        if not isinstance(item, dict):
            raise ValueError("inspection entry invalid")
        row_id, defect = item.get("row_id"), item.get("defect")
        if not isinstance(row_id, str) or row_id in seen or type(defect) is not bool:
            raise ValueError("inspection receipt invalid or duplicated")
        seen.add(row_id)
        if row_id not in selected:
            raise ValueError("inspection IDs do not match fixed random draw")
        observed.append({"row_id": row_id, "region": before_rows[row_id].values["region"], "defect": defect})
    if seen != selected:
        raise ValueError("inspection IDs do not match fixed random draw")
    deleted = set(before_rows) - set(after_rows)
    if draw.get("deletion_policy") == "lowest_five_unselected_east_ids_after_draw":
        expected = set(sorted(set(by_region["east"]) - selected)[:5])
        if deleted != expected:
            raise ValueError("deletion does not match declared post-draw policy")
    else:
        raise ValueError("unsupported deletion policy")
    payload = {
        "before_snapshot": before.workspace.snapshot.snapshot_id,
        "after_snapshot": after.workspace.snapshot.snapshot_id,
        "before_run_key": before.run_key,
        "after_run_key": after.run_key,
        "design": draw["design"],
        "deletion_policy": draw["deletion_policy"],
        "population_before_by_region": dict(sorted(Counter(row.values["region"] for row in before_rows.values()).items())),
        "population_after_by_region": dict(sorted(Counter(row.values["region"] for row in after_rows.values()).items())),
        "sample_by_region": dict(sorted(Counter(item["region"] for item in observed).items())),
        "sample_defects_by_region": dict(sorted(Counter(item["region"] for item in observed if item["defect"]).items())),
        "deleted_row_ids": sorted(deleted),
        "deleted_inspected_count": len(deleted & selected),
        "surviving_inspected_count": len(selected & set(after_rows)),
        "inspections": sorted(observed, key=lambda item: item["row_id"]),
        "population_upper_bounds": None,
        "publication_decision": None,
    }
    payload["evidence_sha256"] = sha256_text(canonical_json(payload))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect a pre-deletion quality draw against verified lake snapshots")
    parser.add_argument("--root", required=True)
    parser.add_argument("--draw", required=True)
    args = parser.parse_args()
    print(canonical_json(inspect_deletion_cohort(args.root, args.draw)))


if __name__ == "__main__":
    main()
