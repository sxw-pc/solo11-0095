"""Two-scenario power procurement for a reconciled synthetic audit batch.

The module turns the real row count of an executed audit into the audit power
demand of every scenario.  It then signs two independent three-period (at most
four-period) contracts:

* ``cost_guard`` minimises the worst scenario monetary cost;
* ``carbon_guard`` minimises the worst scenario emissions.

The first period is non-anticipatory: both scenarios must use exactly the same
grid/diesel purchase.  Afterwards the scenario is revealed and each branch may
be repaired independently.  The solver is exact: the shared first period is
enumerated and each scenario suffix is solved with a bounded dynamic program
over the battery state.  The returned certificate therefore contains all
feasible first decisions and the exact best suffix for both objectives, which
can be checked offline without trusting a pre-computed witness.

All values in the bundled examples are synthetic and the module is fully
offline (standard library only).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import execute_audit
from .errors import AuditError
from .util import pretty_json

MAX_SCENARIOS = 3
MAX_PERIODS = 4
MAX_DISPATCH_PER_PERIOD = 8
REVEALED_AFTER_PERIOD = 1

_DEFAULT_WORKSPACE = (
    Path(__file__).resolve().parents[2] / "examples" / "audit_power" / "lake"
)


# ---------------------------------------------------------------------------
# Compatibility preview
# ---------------------------------------------------------------------------


def _check_audit_binding(data: dict, result) -> int:
    """Verify snapshot/run key/reconciliation and return the audited row count."""
    if not isinstance(data, dict):
        raise ValueError("power plan input must be a JSON object")
    snapshot_id = data.get("snapshot_id")
    run_key = data.get("run_key")
    if result.workspace.snapshot.snapshot_id != snapshot_id or result.run_key != run_key:
        raise ValueError("power plan is bound to a different audit")
    if result.stats["diffs"] != 0:
        raise ValueError("unreconciled audit workload cannot schedule a power plan")
    rows = result.stats["rows"]
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise ValueError("audit row count must be a non-negative integer")
    return rows


def _normal_demand(data: dict, rows: int) -> list[int]:
    normal = data["scenarios"]["normal"]
    energy = data.get("energy_per_audited_row")
    if isinstance(energy, bool) or not isinstance(energy, int) or energy < 0:
        raise ValueError("energy_per_audited_row must be a non-negative integer")
    return [base + rows * energy for base in normal["base_load"]]


def normal_preview(data: dict, result) -> dict:
    """Legacy normal-only greedy preview (kept for backwards compatibility).

    It intentionally ignores the outage scenario, battery and final reserve.
    Use :func:`optimize_intake` for the signed two-scenario plan.
    """
    rows = _check_audit_binding(data, result)
    demand = _normal_demand(data, rows)
    normal = data["scenarios"]["normal"]
    dispatch = []
    for t, load in enumerate(demand):
        grid = min(load, normal["grid_max"][t])
        dispatch.append({"period": t + 1, "grid": grid, "diesel": load - grid})
    cost = sum(
        row["grid"] * data["grid_cost"][t]
        + row["diesel"] * data["diesel_cost"][t]
        for t, row in enumerate(dispatch)
    )
    return {
        "snapshot_id": data["snapshot_id"],
        "audited_rows": rows,
        "normal_demand": demand,
        "normal_dispatch": dispatch,
        "normal_cost": cost,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _as_int(value: Any, label: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be <= {maximum}")
    return value


def _int_series(
    value: Any,
    label: str,
    *,
    length: int | None = None,
    minimum: int = 0,
    maximum: int | None = None,
) -> list[int]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if not value:
        raise ValueError(f"{label} must not be empty")
    result = [
        _as_int(item, f"{label}[{index}]", minimum=minimum, maximum=maximum)
        for index, item in enumerate(value)
    ]
    if length is not None and len(result) != length:
        raise ValueError(f"{label} must contain exactly {length} periods")
    return result


def _require_mapping(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def validate_intake(data: dict) -> dict[str, Any]:
    """Validate the bytes of a power intake fixture and normalise its values.

    Raises ``ValueError`` for malformed parameters.  Input mapping insertion
    order is irrelevant: scenarios are ordered lexicographically internally.
    """
    data = _require_mapping(data, "fixture")
    for key in ("snapshot_id", "run_key"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"{key} must be a non-empty string")

    energy = _as_int(
        data.get("energy_per_audited_row"),
        "energy_per_audited_row",
        minimum=0,
    )
    if data.get("revealed_after_period") != REVEALED_AFTER_PERIOD:
        raise ValueError("revealed_after_period must be 1")

    diesel_max = _as_int(
        data.get("diesel_max_each_period"),
        "diesel_max_each_period",
        maximum=MAX_DISPATCH_PER_PERIOD,
    )

    battery = _require_mapping(data.get("battery"), "battery")
    initial = _as_int(battery.get("initial"), "battery.initial")
    capacity = _as_int(battery.get("capacity"), "battery.capacity")
    final_reserve = _as_int(battery.get("final_reserve"), "battery.final_reserve")
    if initial > capacity:
        raise ValueError("battery.initial must not exceed battery.capacity")

    grid_cost = _int_series(data.get("grid_cost"), "grid_cost")
    periods = len(grid_cost)
    if not 1 <= periods <= MAX_PERIODS:
        raise ValueError(f"period count must be between 1 and {MAX_PERIODS}")
    diesel_cost = _int_series(data.get("diesel_cost"), "diesel_cost", length=periods)
    grid_emissions = _int_series(
        data.get("grid_emissions"), "grid_emissions", length=periods
    )
    diesel_emissions = _int_series(
        data.get("diesel_emissions"), "diesel_emissions", length=periods
    )

    scenarios_raw = _require_mapping(data.get("scenarios"), "scenarios")
    if not 1 <= len(scenarios_raw) <= MAX_SCENARIOS:
        raise ValueError(f"scenario count must be between 1 and {MAX_SCENARIOS}")
    scenarios: dict[str, dict[str, list[int]]] = {}
    for name in sorted(scenarios_raw):
        if not isinstance(name, str) or not name:
            raise ValueError("scenario names must be non-empty strings")
        scenario = _require_mapping(scenarios_raw[name], f"scenarios.{name}")
        scenarios[name] = {
            "base_load": _int_series(
                scenario.get("base_load"), f"scenarios.{name}.base_load", length=periods
            ),
            "grid_max": _int_series(
                scenario.get("grid_max"),
                f"scenarios.{name}.grid_max",
                length=periods,
                maximum=MAX_DISPATCH_PER_PERIOD,
            ),
        }

    scenario_names = sorted(scenarios)
    first_base = scenarios[scenario_names[0]]["base_load"][0]
    first_grid = scenarios[scenario_names[0]]["grid_max"][0]
    for name in scenario_names[1:]:
        if scenarios[name]["base_load"][0] != first_base:
            raise ValueError("all scenarios must share the first-period base_load")
        if scenarios[name]["grid_max"][0] != first_grid:
            raise ValueError("all scenarios must share the first-period grid_max")

    contracts = _require_mapping(data.get("contracts"), "contracts")
    normalised_contracts: dict[str, dict[str, Any]] = {}
    expected = {
        "cost_guard": ("minimize_worst_scenario_cost", "max_worst_cost"),
        "carbon_guard": (
            "minimize_worst_scenario_emissions",
            "max_worst_emissions",
        ),
    }
    for name, (objective, limit_key) in expected.items():
        contract = _require_mapping(contracts.get(name), f"contracts.{name}")
        if contract.get("objective") != objective:
            raise ValueError(f"contracts.{name}.objective must be {objective!r}")
        limit = _as_int(contract.get(limit_key), f"contracts.{name}.{limit_key}")
        normalised_contracts[name] = {
            "objective": objective,
            limit_key: limit,
        }

    return {
        "snapshot_id": data["snapshot_id"],
        "run_key": data["run_key"],
        "energy_per_audited_row": energy,
        "battery": {
            "initial": initial,
            "capacity": capacity,
            "final_reserve": final_reserve,
        },
        "diesel_max_each_period": diesel_max,
        "grid_cost": grid_cost,
        "diesel_cost": diesel_cost,
        "grid_emissions": grid_emissions,
        "diesel_emissions": diesel_emissions,
        "periods": periods,
        "scenarios": scenarios,
        "scenario_order": scenario_names,
        "contracts": normalised_contracts,
    }


# ---------------------------------------------------------------------------
# Exact solver
# ---------------------------------------------------------------------------


def _suffix_dp(
    context: dict[str, Any], scenario: str, metric: str
) -> list[dict[int, tuple[int, tuple[tuple[int, int], ...]]]]:
    """Exact best suffix for one scenario.

    ``dp[t][b]`` is the best value and lexicographically smallest remediation
    vector ``((grid, diesel), ...)`` for periods ``t .. P-1`` given battery
    state ``b`` before period ``t``.  ``t=0`` (the shared first period) is
    never computed here.  ``None`` entries mean the suffix cannot meet demand,
    stay within capacity, or leave the final reserve.
    """
    periods = context["periods"]
    capacity = context["battery"]["capacity"]
    reserve = context["battery"]["final_reserve"]
    demand = context["demand"][scenario]
    grid_max = context["scenarios"][scenario]["grid_max"]
    diesel_max = context["diesel_max_each_period"]
    if metric == "cost":
        grid_rate = context["grid_cost"]
        diesel_rate = context["diesel_cost"]
    else:
        grid_rate = context["grid_emissions"]
        diesel_rate = context["diesel_emissions"]

    # Each entry is Optional[(value, vector)].
    dp: list[dict[int, tuple[int, tuple[tuple[int, int], ...]] | None]] = [
        {} for _ in range(periods + 1)
    ]
    for battery in range(capacity + 1):
        dp[periods][battery] = (
            (0, ()) if battery >= reserve else None
        )

    for t in range(periods - 1, 0, -1):
        current: dict[int, tuple[int, tuple[tuple[int, int], ...]] | None] = {}
        for battery in range(capacity + 1):
            best: tuple[int, tuple[tuple[int, int], ...]] | None = None
            for grid in range(min(grid_max[t], MAX_DISPATCH_PER_PERIOD) + 1):
                for diesel in range(min(diesel_max, MAX_DISPATCH_PER_PERIOD) + 1):
                    after = battery + grid + diesel - demand[t]
                    if after < 0 or after > capacity:
                        continue
                    tail = dp[t + 1].get(after)
                    if tail is None:
                        continue
                    value = (
                        grid * grid_rate[t]
                        + diesel * diesel_rate[t]
                        + tail[0]
                    )
                    vector = ((grid, diesel),) + tail[1]
                    candidate = (value, vector)
                    if best is None or candidate < best:
                        best = candidate
            current[battery] = best
        dp[t] = current
    return dp  # type: ignore[return-value]


def _build_scenario_plan(
    context: dict[str, Any],
    scenario: str,
    first_grid: int,
    first_diesel: int,
    suffix: tuple[tuple[int, int], ...],
) -> dict[str, Any]:
    """Materialise a plan, checking battery conservation as it goes."""
    periods = context["periods"]
    demand = context["demand"][scenario]
    battery = context["battery"]["initial"]
    capacity = context["battery"]["capacity"]
    dispatch: list[dict[str, int]] = []
    for t in range(periods):
        if t == 0:
            grid, diesel = first_grid, first_diesel
        else:
            grid, diesel = suffix[t - 1]
        battery += grid + diesel - demand[t]
        if battery < 0 or battery > capacity:
            raise ValueError("internal solver error: battery state out of bounds")
        dispatch.append(
            {
                "period": t + 1,
                "grid": grid,
                "diesel": diesel,
                "battery": battery,
                "demand": demand[t],
            }
        )
    cost = sum(
        row["grid"] * context["grid_cost"][row["period"] - 1]
        + row["diesel"] * context["diesel_cost"][row["period"] - 1]
        for row in dispatch
    )
    emissions = sum(
        row["grid"] * context["grid_emissions"][row["period"] - 1]
        + row["diesel"] * context["diesel_emissions"][row["period"] - 1]
        for row in dispatch
    )
    return {
        "dispatch": dispatch,
        "later": [
            {"grid": row["grid"], "diesel": row["diesel"]}
            for row in dispatch[1:]
        ],
        "battery": [row["battery"] for row in dispatch],
        "cost": cost,
        "emissions": emissions,
    }


def _objective_entry(
    context: dict[str, Any],
    first_grid: int,
    first_diesel: int,
    metric: str,
    suffix_dp: dict[str, list[dict[int, tuple[int, tuple[tuple[int, int], ...]]]]],
    battery_after_first: int,
) -> dict[str, Any]:
    plans: dict[str, dict[str, Any]] = {}
    for scenario in context["scenario_order"]:
        found = suffix_dp[scenario][1].get(battery_after_first)
        if found is None:  # pragma: no cover - guarded by caller
            raise ValueError("internal solver error: missing suffix")
        plans[scenario] = _build_scenario_plan(
            context, scenario, first_grid, first_diesel, found[1]
        )
    worst_cost = max(plans[name]["cost"] for name in plans)
    worst_emissions = max(plans[name]["emissions"] for name in plans)
    return {
        "objective": metric,
        "scenarios": plans,
        "worst_cost": worst_cost,
        "worst_emissions": worst_emissions,
    }


def _plan_tie_key(entry: dict[str, Any], branch: str) -> tuple:
    """Stable tie-break: objective, then first grid, then first diesel.

    The suffix vectors are appended last so that two otherwise identical
    first decisions (which cannot differ here, because the state is fully
    determined by the first purchase) still order deterministically.
    """
    branch_data = entry[branch]
    vectors = tuple(
        tuple(
            (row["grid"], row["diesel"])
            for row in branch_data["scenarios"][name]["later"]
        )
        for name in sorted(branch_data["scenarios"])
    )
    objective = (
        entry["minimum_worst_cost"]
        if branch == "by_cost"
        else entry["minimum_worst_emissions"]
    )
    return (objective, entry["grid"], entry["diesel"], vectors)


def build_certificate(
    context: dict[str, Any], audited_rows: int
) -> dict[str, Any]:
    """Exhaust every feasible shared first decision and solve exact suffixes."""
    periods = context["periods"]
    capacity = context["battery"]["capacity"]
    initial = context["battery"]["initial"]
    diesel_max = context["diesel_max_each_period"]
    first_demand = context["demand"][context["scenario_order"][0]][0]
    first_grid_max = context["scenarios"][context["scenario_order"][0]]["grid_max"][0]

    suffix_cost = {
        name: _suffix_dp(context, name, "cost") for name in context["scenario_order"]
    }
    suffix_emissions = {
        name: _suffix_dp(context, name, "emissions")
        for name in context["scenario_order"]
    }

    feasible: list[dict[str, Any]] = []
    infeasible: list[dict[str, Any]] = []
    for first_grid in range(first_grid_max + 1):
        for first_diesel in range(diesel_max + 1):
            battery_after_first = initial + first_grid + first_diesel - first_demand
            if battery_after_first < 0 or battery_after_first > capacity:
                infeasible.append(
                    {
                        "grid": first_grid,
                        "diesel": first_diesel,
                        "reason": (
                            "battery state after period 1 is "
                            f"{battery_after_first}, outside 0..{capacity}"
                        ),
                    }
                )
                continue

            missing = [
                name
                for name in context["scenario_order"]
                if suffix_cost[name][1].get(battery_after_first) is None
                or suffix_emissions[name][1].get(battery_after_first) is None
            ]
            if missing:
                infeasible.append(
                    {
                        "grid": first_grid,
                        "diesel": first_diesel,
                        "reason": (
                            "cannot meet full-horizon demand for scenario(s): "
                            + ", ".join(missing)
                        ),
                    }
                )
                continue

            by_cost = _objective_entry(
                context,
                first_grid,
                first_diesel,
                "cost",
                suffix_cost,
                battery_after_first,
            )
            by_emissions = _objective_entry(
                context,
                first_grid,
                first_diesel,
                "emissions",
                suffix_emissions,
                battery_after_first,
            )
            feasible.append(
                {
                    "grid": first_grid,
                    "diesel": first_diesel,
                    "battery_after_first": battery_after_first,
                    "by_cost": by_cost,
                    "by_emissions": by_emissions,
                    "minimum_worst_cost": by_cost["worst_cost"],
                    "minimum_worst_emissions": by_emissions["worst_emissions"],
                    "cost_optimised_worst_emissions": by_cost["worst_emissions"],
                    "emissions_optimised_worst_cost": by_emissions["worst_cost"],
                }
            )

    bounds_view = [
        {
            "grid": entry["grid"],
            "diesel": entry["diesel"],
            "minimum_worst_cost": entry["minimum_worst_cost"],
            "minimum_worst_emissions": entry["minimum_worst_emissions"],
        }
        for entry in sorted(
            feasible, key=lambda item: (item["minimum_worst_cost"], item["grid"], item["diesel"])
        )
    ]
    return {
        "method": (
            "exhaustive shared first-period enumeration with exact bounded "
            "battery-state dynamic programming for each scenario suffix"
        ),
        "limits": {
            "max_scenarios": MAX_SCENARIOS,
            "max_periods": MAX_PERIODS,
            "max_dispatch_per_period": MAX_DISPATCH_PER_PERIOD,
        },
        "snapshot_id": context["snapshot_id"],
        "run_key": context["run_key"],
        "audited_rows": audited_rows,
        "periods": periods,
        "scenario_order": list(context["scenario_order"]),
        "demand": {name: list(values) for name, values in context["demand"].items()},
        "first_period": {
            "base_load": context["scenarios"][context["scenario_order"][0]][
                "base_load"
            ][0],
            "grid_max": first_grid_max,
            "diesel_max": diesel_max,
        },
        "feasible_first_decisions": feasible,
        "infeasible_first_decisions": infeasible,
        "all_first_stage_exact_suffix_bounds": bounds_view,
        "minimum_worst_cost": min(
            (entry["minimum_worst_cost"] for entry in feasible), default=None
        ),
        "minimum_worst_emissions": min(
            (entry["minimum_worst_emissions"] for entry in feasible), default=None
        ),
    }


# ---------------------------------------------------------------------------
# Contract signing
# ---------------------------------------------------------------------------


def _contract_view(
    context: dict[str, Any],
    name: str,
    entry: dict[str, Any] | None,
    branch: str,
) -> dict[str, Any]:
    contract_spec = context["contracts"][name]
    objective = contract_spec["objective"]
    if name == "cost_guard":
        limit_key, metric_name = "max_worst_cost", "cost"
    else:
        limit_key, metric_name = "max_worst_emissions", "emissions"
    limit = contract_spec[limit_key]
    actual_worst = (
        entry["minimum_worst_cost"]
        if name == "cost_guard"
        else entry["minimum_worst_emissions"]
    ) if entry else None

    view: dict[str, Any] = {
        "name": name,
        "objective": objective,
        limit_key: limit,
        "status": "rejected",
        "signed": False,
        "reason": "",
        "common_period_1": None,
        "battery_after_first": None,
        "scenarios": {},
        "worst_cost": None,
        "worst_emissions": None,
        "worst_cost_scenario": None,
        "worst_emissions_scenario": None,
    }

    if entry is None:
        view["reason"] = (
            "rejected: no feasible shared first-period decision can supply "
            "every scenario through the full horizon and meet the final reserve"
        )
        return view

    branch_data = entry[branch]
    plans = branch_data["scenarios"]
    view["common_period_1"] = {"grid": entry["grid"], "diesel": entry["diesel"]}
    view["battery_after_first"] = entry["battery_after_first"]
    view["scenarios"] = plans
    for scenario in context["scenario_order"]:
        view[f"{scenario}_later"] = plans[scenario]["later"]
    view["worst_cost"] = branch_data["worst_cost"]
    view["worst_emissions"] = branch_data["worst_emissions"]
    view["worst_cost_scenario"] = max(
        context["scenario_order"], key=lambda item: plans[item]["cost"]
    )
    view["worst_emissions_scenario"] = max(
        context["scenario_order"], key=lambda item: plans[item]["emissions"]
    )

    passed = actual_worst is not None and actual_worst <= limit
    view["signed"] = bool(passed)
    view["status"] = "signed" if passed else "rejected"
    if passed:
        view["reason"] = (
            f"signed: worst {metric_name} {actual_worst} is within the "
            f"authorised limit {limit}"
        )
    else:
        view["reason"] = (
            f"rejected: worst {metric_name} {actual_worst} exceeds the "
            f"authorised limit {limit}"
        )
    return view


def _select_entry(certificate: dict[str, Any], branch: str) -> dict[str, Any] | None:
    feasible = certificate["feasible_first_decisions"]
    if not feasible:
        return None
    return min(feasible, key=lambda entry: _plan_tie_key(entry, branch))


def optimize_intake(data: dict, result) -> dict[str, Any]:
    """Build the signed dual-scenario plan and its optimality certificate.

    ``data`` is the parsed intake fixture and ``result`` is an ``AuditResult``
    produced by :func:`lakedel.engine.execute_audit`.  The audit row count is
    read from ``result.stats['rows']`` so changes to the source lake table or
    delete vectors flow into demand, the feasible region and both contracts.
    """
    rows = _check_audit_binding(data, result)
    context = validate_intake(data)
    context["demand"] = {
        name: [
            base + rows * context["energy_per_audited_row"]
            for base in context["scenarios"][name]["base_load"]
        ]
        for name in context["scenario_order"]
    }
    context["snapshot_id"] = data["snapshot_id"]
    context["run_key"] = data["run_key"]

    certificate = build_certificate(context, rows)
    cost_entry = _select_entry(certificate, "by_cost")
    carbon_entry = _select_entry(certificate, "by_emissions")
    cost_contract = _contract_view(context, "cost_guard", cost_entry, "by_cost")
    carbon_contract = _contract_view(
        context, "carbon_guard", carbon_entry, "by_emissions"
    )

    report = {
        "snapshot_id": data["snapshot_id"],
        "run_key": data["run_key"],
        "audited_rows": rows,
        "periods": context["periods"],
        "scenario_order": list(context["scenario_order"]),
        "demand": {name: list(values) for name, values in context["demand"].items()},
        "cost_guard": cost_contract,
        "carbon_guard": carbon_contract,
        "contracts": {
            "cost_guard": cost_contract,
            "carbon_guard": carbon_contract,
        },
        "certificate": certificate,
    }
    return report


# A few readable aliases for callers that prefer plan/contract wording.
def sign_contracts(data: dict, result) -> dict[str, Any]:
    return optimize_intake(data, result)


def build_power_plan(data: dict, result) -> dict[str, Any]:
    return optimize_intake(data, result)


def optimize_fixture(
    fixture: str | Path,
    workspace: str | Path | None = None,
    result=None,
) -> dict[str, Any]:
    """Load an intake fixture (and optionally an audit result) and optimise it."""
    fixture_path = Path(fixture)
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    if result is None:
        audit_root = Path(workspace) if workspace is not None else _DEFAULT_WORKSPACE
        result = execute_audit(audit_root)
    return optimize_intake(data, result)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sign synthetic audit-batch power procurement contracts"
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=_DEFAULT_WORKSPACE,
        help="isolated lake snapshot to audit (default: bundled audit_power lake)",
    )
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.fixture.read_text(encoding="utf-8"))
        result = execute_audit(args.workspace)
        report = optimize_intake(data, result)
    except (ValueError, OSError, AuditError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    print(pretty_json(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
