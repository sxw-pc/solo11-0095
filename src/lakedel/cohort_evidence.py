"""Read a fixed synthetic inspection sample against verified visible lake rows."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from .engine import execute_audit
from .util import canonical_json, sha256_text


def inspect_sample(workspace: str | Path, manifest_path: str | Path) -> dict:
    audit = execute_audit(workspace)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("snapshot_id") != audit.workspace.snapshot.snapshot_id:
        raise ValueError("sample snapshot does not match audited snapshot")
    visible = {
        row.row_id: row
        for row, decision in zip(audit.workspace.rows, audit.decisions)
        if decision.visible and row.row_id is not None
    }
    if len(visible) != sum(decision.visible for decision in audit.decisions):
        raise ValueError("visible row identity is not unique")
    inspections = manifest.get("inspections")
    if not isinstance(inspections, list) or not inspections:
        raise ValueError("inspection list is empty")
    observed: list[dict] = []
    seen: set[str] = set()
    for item in inspections:
        row_id = item.get("row_id")
        defect = item.get("defect")
        if not isinstance(row_id, str) or row_id in seen:
            raise ValueError("inspection row identity is missing or duplicated")
        if row_id not in visible:
            raise ValueError("inspection row is not auditable and visible")
        if type(defect) is not bool:
            raise ValueError("inspection defect must be boolean")
        seen.add(row_id)
        observed.append({
            "row_id": row_id,
            "region": visible[row_id].values["region"],
            "defect": defect,
        })
    population = dict(sorted(Counter(row.values["region"] for row in visible.values()).items()))
    sample = dict(sorted(Counter(item["region"] for item in observed).items()))
    if set(population) != set(sample):
        raise ValueError("each visible region needs at least one inspected row")
    payload = {
        "snapshot_id": audit.workspace.snapshot.snapshot_id,
        "run_key": audit.run_key,
        "population_by_region": population,
        "sample_by_region": sample,
        "inspections": sorted(observed, key=lambda item: item["row_id"]),
        "sample_defects": sum(item["defect"] for item in observed),
        "population_defects_estimate": None,
        "publication_decision": None,
    }
    payload["evidence_sha256"] = sha256_text(canonical_json(payload))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect a fixed local quality sample")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    print(canonical_json(inspect_sample(args.workspace, args.manifest)))


if __name__ == "__main__":
    main()
