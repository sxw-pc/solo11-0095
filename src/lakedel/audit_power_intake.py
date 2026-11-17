"""Normal-case power preview for a synthetic snapshot audit batch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import execute_audit


def normal_preview(data: dict, result) -> dict:
    if result.workspace.snapshot.snapshot_id != data["snapshot_id"] or result.run_key != data["run_key"]:
        raise ValueError("power plan is bound to a different audit")
    if result.stats["diffs"] != 0:
        raise ValueError("unreconciled audit workload cannot schedule a power plan")
    normal = data["scenarios"]["normal"]
    rows = result.stats["rows"]
    demand = [base + rows * data["energy_per_audited_row"] for base in normal["base_load"]]
    dispatch = []
    for t, load in enumerate(demand):
        grid = min(load, normal["grid_max"][t])
        dispatch.append({"period": t + 1, "grid": grid, "diesel": load - grid})
    cost = sum(row["grid"] * data["grid_cost"][t] + row["diesel"] * data["diesel_cost"][t]
               for t, row in enumerate(dispatch))
    return {"snapshot_id": data["snapshot_id"], "audited_rows": rows,
            "normal_demand": demand, "normal_dispatch": dispatch, "normal_cost": cost}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview synthetic audit-batch power")
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args(argv)
    data = json.loads(args.fixture.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[2]
    result = execute_audit(root / "examples/audit_power/lake")
    print(json.dumps(normal_preview(data, result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
