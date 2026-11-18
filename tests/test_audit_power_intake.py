import copy
import hashlib
import itertools
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lakedel.audit_power_intake import (
    MAX_DISPATCH_PER_PERIOD,
    build_certificate,
    build_power_plan,
    main,
    normal_preview,
    optimize_fixture,
    optimize_intake,
    sign_contracts,
    validate_intake,
)
from lakedel.engine import execute_audit


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/audit_power/intake.json"
WITNESS = ROOT / "examples/audit_power/witness.json"
LAKE = ROOT / "examples/audit_power/lake"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def load_witness() -> dict:
    return json.loads(WITNESS.read_text(encoding="utf-8"))


def audit(workspace: Path = LAKE):
    return execute_audit(workspace)


def demands(data: dict, rows: int) -> dict[str, list[int]]:
    energy = data["energy_per_audited_row"]
    return {
        name: [base + rows * energy for base in scenario["base_load"]]
        for name, scenario in data["scenarios"].items()
    }


def assert_contract_feasible(test, data: dict, contract: dict, rows: int) -> None:
    """Independent replay of a signed contract against the raw fixture."""
    demand = demands(data, rows)
    capacity = data["battery"]["capacity"]
    reserve = data["battery"]["final_reserve"]
    first = contract["common_period_1"]
    test.assertGreaterEqual(first["grid"], 0)
    test.assertGreaterEqual(first["diesel"], 0)
    for name, scenario in data["scenarios"].items():
        plan = contract["scenarios"][name]
        charge = data["battery"]["initial"]
        test.assertEqual(plan["dispatch"][0]["grid"], first["grid"])
        test.assertEqual(plan["dispatch"][0]["diesel"], first["diesel"])
        for t, row in enumerate(plan["dispatch"]):
            test.assertEqual(row["period"], t + 1)
            test.assertIsInstance(row["grid"], int)
            test.assertIsInstance(row["diesel"], int)
            test.assertGreaterEqual(row["grid"], 0)
            test.assertGreaterEqual(row["diesel"], 0)
            test.assertLessEqual(row["grid"], scenario["grid_max"][t])
            test.assertLessEqual(row["diesel"], data["diesel_max_each_period"])
            test.assertLessEqual(row["grid"], MAX_DISPATCH_PER_PERIOD)
            test.assertLessEqual(row["diesel"], MAX_DISPATCH_PER_PERIOD)
            charge += row["grid"] + row["diesel"] - demand[name][t]
            test.assertGreaterEqual(charge, 0)
            test.assertLessEqual(charge, capacity)
            test.assertEqual(row["battery"], charge)
            test.assertEqual(row["demand"], demand[name][t])
        test.assertGreaterEqual(charge, reserve)
        cost = sum(
            row["grid"] * data["grid_cost"][t]
            + row["diesel"] * data["diesel_cost"][t]
            for t, row in enumerate(plan["dispatch"])
        )
        emissions = sum(
            row["grid"] * data["grid_emissions"][t]
            + row["diesel"] * data["diesel_emissions"][t]
            for t, row in enumerate(plan["dispatch"])
        )
        test.assertEqual(plan["cost"], cost)
        test.assertEqual(plan["emissions"], emissions)
    costs = [contract["scenarios"][name]["cost"] for name in data["scenarios"]]
    emissions = [contract["scenarios"][name]["emissions"] for name in data["scenarios"]]
    test.assertEqual(contract["worst_cost"], max(costs))
    test.assertEqual(contract["worst_emissions"], max(emissions))


def brute_force_first_periods(data: dict, rows: int) -> list[dict]:
    """Offline reference solver: enumerate every dispatch vector exactly."""
    demand = demands(data, rows)
    capacity = data["battery"]["capacity"]
    reserve = data["battery"]["final_reserve"]
    diesel_max = data["diesel_max_each_period"]
    periods = len(data["grid_cost"])
    scenarios = sorted(data["scenarios"])
    first_load = demand[scenarios[0]][0]
    first_grid_max = data["scenarios"][scenarios[0]]["grid_max"][0]

    suffixes: dict[str, list[tuple[tuple[int, int], ...]]] = {}
    for name in scenarios:
        per_period = [
            [
                (grid, diesel)
                for grid in range(data["scenarios"][name]["grid_max"][t] + 1)
                for diesel in range(diesel_max + 1)
            ]
            for t in range(1, periods)
        ]
        suffixes[name] = list(itertools.product(*per_period))

    results = []
    for first_grid in range(first_grid_max + 1):
        for first_diesel in range(diesel_max + 1):
            start = data["battery"]["initial"] + first_grid + first_diesel - first_load
            if not 0 <= start <= capacity:
                continue
            best: dict[str, dict[str, int]] = {}
            possible = True
            for name in scenarios:
                best[name] = {"cost": None, "emissions": None}
                for suffix in suffixes[name]:
                    battery = start
                    ok = True
                    cost = emissions = 0
                    for index, (grid, diesel) in enumerate(suffix):
                        t = index + 1
                        battery += grid + diesel - demand[name][t]
                        if not 0 <= battery <= capacity:
                            ok = False
                            break
                        cost += (
                            grid * data["grid_cost"][t]
                            + diesel * data["diesel_cost"][t]
                        )
                        emissions += (
                            grid * data["grid_emissions"][t]
                            + diesel * data["diesel_emissions"][t]
                        )
                    if not ok or battery < reserve:
                        continue
                    cost += (
                        first_grid * data["grid_cost"][0]
                        + first_diesel * data["diesel_cost"][0]
                    )
                    emissions += (
                        first_grid * data["grid_emissions"][0]
                        + first_diesel * data["diesel_emissions"][0]
                    )
                    for metric, value in (("cost", cost), ("emissions", emissions)):
                        current = best[name][metric]
                        if current is None or value < current:
                            best[name][metric] = value
                if best[name]["cost"] is None:
                    possible = False
                    break
            if not possible:
                continue
            results.append(
                {
                    "grid": first_grid,
                    "diesel": first_diesel,
                    "minimum_worst_cost": max(item["cost"] for item in best.values()),
                    "minimum_worst_emissions": max(
                        item["emissions"] for item in best.values()
                    ),
                }
            )
    return results


class AuditPowerPreviewTests(unittest.TestCase):
    def test_greedy_preview_ignores_outage_and_battery(self):
        data = load_fixture()
        result = audit()
        report = normal_preview(data, result)
        self.assertEqual(report["audited_rows"], 2)
        self.assertEqual(report["normal_demand"], [4, 3, 4])
        self.assertEqual(report["normal_cost"], 26)
        # The greedy preview never charges diesel beyond the per-period cap and
        # never pre-charges for the outage: period 2 of the outage needs 6 units
        # but can only draw 1 grid + 3 diesel = 4 immediately.
        self.assertEqual(
            sum(row["diesel"] for row in report["normal_dispatch"]), 0
        )
        outage = data["scenarios"]["outage"]
        self.assertGreater(
            outage["base_load"][1] + 2 * data["energy_per_audited_row"],
            outage["grid_max"][1] + data["diesel_max_each_period"],
        )

    def test_bundled_witness_matches_signed_contracts(self):
        data = load_fixture()
        witness = load_witness()
        result = audit()
        report = optimize_intake(data, result)
        for name in ("cost_guard", "carbon_guard"):
            contract = report["contracts"][name]
            expected = witness[name]
            self.assertEqual(contract["common_period_1"], expected["common_period_1"])
            self.assertEqual(contract["worst_cost"], expected["worst_cost"])
            self.assertEqual(contract["worst_emissions"], expected["worst_emissions"])
            for scenario in ("normal", "outage"):
                self.assertEqual(
                    contract["scenarios"][scenario]["later"], expected[f"{scenario}_later"]
                )
            assert_contract_feasible(self, data, contract, result.stats["rows"])

    def test_cost_and_carbon_guards_are_opposite_tradeoffs(self):
        data = load_fixture()
        result = audit()
        report = optimize_intake(data, result)
        cost_contract = report["cost_guard"]
        carbon_contract = report["carbon_guard"]
        self.assertEqual(cost_contract["worst_cost"], 63)
        self.assertEqual(cost_contract["worst_emissions"], 79)
        self.assertEqual(carbon_contract["worst_emissions"], 77)
        self.assertEqual(carbon_contract["worst_cost"], 69)
        # Signing the cheapest plan is not the cleanest plan and vice versa.
        self.assertGreater(cost_contract["worst_emissions"], carbon_contract["worst_emissions"])
        self.assertLess(cost_contract["worst_cost"], carbon_contract["worst_cost"])
        self.assertNotEqual(
            cost_contract["common_period_1"], carbon_contract["common_period_1"]
        )
        self.assertEqual(cost_contract["status"], "signed")
        self.assertEqual(carbon_contract["status"], "signed")
        self.assertIn("63", cost_contract["reason"])
        self.assertIn("77", carbon_contract["reason"])

    def test_first_period_is_non_anticipatory(self):
        data = load_fixture()
        result = audit()
        report = optimize_intake(data, result)
        for name in ("cost_guard", "carbon_guard"):
            contract = report["contracts"][name]
            normal = contract["scenarios"]["normal"]["dispatch"][0]
            outage = contract["scenarios"]["outage"]["dispatch"][0]
            self.assertEqual(
                (normal["grid"], normal["diesel"], normal["battery"]),
                (outage["grid"], outage["diesel"], outage["battery"]),
            )
            self.assertEqual(
                contract["common_period_1"],
                {"grid": normal["grid"], "diesel": normal["diesel"]},
            )
            # Later periods are allowed to diverge once the scenario is known.
            self.assertNotEqual(
                contract["scenarios"]["normal"]["later"],
                contract["scenarios"]["outage"]["later"],
            )

    def test_certificate_matches_independent_brute_force(self):
        data = load_fixture()
        result = audit()
        report = optimize_intake(data, result)
        certificate = report["certificate"]
        expected = {
            (row["grid"], row["diesel"]): row
            for row in brute_force_first_periods(data, result.stats["rows"])
        }
        actual = {
            (row["grid"], row["diesel"]): row
            for row in certificate["feasible_first_decisions"]
        }
        self.assertEqual(set(expected), set(actual))
        for key, row in expected.items():
            self.assertEqual(actual[key]["minimum_worst_cost"], row["minimum_worst_cost"])
            self.assertEqual(
                actual[key]["minimum_worst_emissions"], row["minimum_worst_emissions"]
            )
        bounds = {
            (row["grid"], row["diesel"]): row
            for row in certificate["all_first_stage_exact_suffix_bounds"]
        }
        witness_bounds = {
            (row["grid"], row["diesel"]): row
            for row in load_witness()["all_first_stage_exact_suffix_bounds"]
        }
        self.assertEqual(set(bounds), set(witness_bounds))
        for key, row in witness_bounds.items():
            self.assertEqual(bounds[key]["minimum_worst_cost"], row["minimum_worst_cost"])
            self.assertEqual(
                bounds[key]["minimum_worst_emissions"], row["minimum_worst_emissions"]
            )

    def test_cost_plan_emissions_are_not_the_suffix_lower_bound(self):
        """A cost-optimal plan need not minimise its own branch's emissions."""
        data = load_fixture()
        result = audit()
        certificate = optimize_intake(data, result)["certificate"]
        entry = next(
            row
            for row in certificate["feasible_first_decisions"]
            if (row["grid"], row["diesel"]) == (4, 2)
        )
        self.assertEqual(entry["minimum_worst_cost"], 63)
        self.assertEqual(entry["minimum_worst_emissions"], 78)
        self.assertEqual(entry["cost_optimised_worst_emissions"], 79)
        # The certificate stores both, so the 78 lower bound is never confused
        # with the 79 actually realised by the cost-driven suffix schedule.
        self.assertNotEqual(
            entry["cost_optimised_worst_emissions"], entry["minimum_worst_emissions"]
        )

    def test_certificate_is_self_contained_and_offline_checkable(self):
        data = load_fixture()
        result = audit()
        report = optimize_intake(data, result)
        certificate = report["certificate"]
        self.assertEqual(certificate["audited_rows"], 2)
        self.assertEqual(certificate["snapshot_id"], data["snapshot_id"])
        self.assertEqual(certificate["run_key"], data["run_key"])
        self.assertEqual(
            certificate["demand"],
            {"normal": [4, 3, 4], "outage": [4, 6, 4]},
        )
        self.assertTrue(certificate["feasible_first_decisions"])
        self.assertTrue(certificate["infeasible_first_decisions"])
        for row in certificate["feasible_first_decisions"]:
            for branch in ("by_cost", "by_emissions"):
                for scenario in data["scenarios"]:
                    plan = row[branch]["scenarios"][scenario]
                    charge = data["battery"]["initial"]
                    for t, item in enumerate(plan["dispatch"]):
                        charge += (
                            item["grid"]
                            + item["diesel"]
                            - certificate["demand"][scenario][t]
                        )
                        self.assertEqual(item["battery"], charge)
                        self.assertTrue(0 <= charge <= data["battery"]["capacity"])
                    self.assertGreaterEqual(
                        charge, data["battery"]["final_reserve"]
                    )

    def test_result_shape_is_stable_and_aliases_agree(self):
        data = load_fixture()
        result = audit()
        report = optimize_intake(data, result)
        self.assertEqual(report, sign_contracts(data, result))
        self.assertEqual(report, build_power_plan(data, result))
        self.assertEqual(
            optimize_fixture(FIXTURE),
            report,
        )
        self.assertEqual(
            optimize_fixture(FIXTURE, result=result),
            report,
        )


class AuditPowerValidationTests(unittest.TestCase):
    def setUp(self):
        self.data = load_fixture()
        self.result = audit()

    def test_stale_run_key_is_rejected(self):
        data = copy.deepcopy(self.data)
        data["run_key"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "different audit"):
            optimize_intake(data, self.result)

    def test_reconciliation_mismatch_is_rejected(self):
        data = copy.deepcopy(self.data)
        data["snapshot_id"] = "snap_999"
        with self.assertRaisesRegex(ValueError, "different audit"):
            optimize_intake(data, self.result)

    def test_unreconciled_audit_is_rejected(self):
        data = copy.deepcopy(self.data)
        result = audit()
        # Simulate an audit with a diff without touching the real fixture.
        object.__setattr__(result, "stats", dict(result.stats, diffs=1))
        with self.assertRaisesRegex(ValueError, "unreconciled"):
            optimize_intake(data, result)

    def test_input_reordering_does_not_change_the_plan(self):
        reference = optimize_intake(self.data, self.result)
        reordered = {
            key: self.data[key]
            for key in reversed(list(self.data))
        }
        reordered["scenarios"] = {
            name: self.data["scenarios"][name]
            for name in reversed(list(self.data["scenarios"]))
        }
        self.assertEqual(optimize_intake(reordered, self.result), reference)

    def test_invalid_parameters_are_rejected(self):
        mutations = [
            ("grid_cost", [2, 2]),
            ("grid_cost", [2, -1, 3]),
            ("grid_cost", [2, True, 3]),
            ("diesel_cost", [7, 8]),
            ("grid_emissions", "not-a-list"),
            ("energy_per_audited_row", True),
            ("energy_per_audited_row", -1),
            ("diesel_max_each_period", -1),
            ("diesel_max_each_period", MAX_DISPATCH_PER_PERIOD + 1),
            ("revealed_after_period", 2),
            ("battery", {"initial": 4, "capacity": 3, "final_reserve": 1}),
            ("battery", {"initial": 1, "capacity": 3}),
            ("scenarios", {"normal": {"base_load": [2, 1, 2]}}),
            (
                "scenarios",
                {
                    "normal": {"base_load": [2, 1, 2], "grid_max": [4, 4, 5]},
                    "outage": {"base_load": [3, 4, 2], "grid_max": [4, 1, 3]},
                },
            ),
            (
                "scenarios",
                {
                    "normal": {"base_load": [2, 1, 2], "grid_max": [4, 4, 5]},
                    "outage": {"base_load": [2, 4, 2], "grid_max": [3, 1, 3]},
                },
            ),
            (
                "scenarios",
                {
                    "normal": {"base_load": [2, 1, 2], "grid_max": [4, 4, 5]},
                    "outage": {"base_load": [2, 4], "grid_max": [4, 1]},
                },
            ),
            ("scenarios", {}),
            ("contracts", {"cost_guard": {"objective": "minimize_worst_scenario_cost"}}),
            (
                "contracts",
                {
                    "cost_guard": {
                        "objective": "minimize_worst_scenario_emissions",
                        "max_worst_cost": 65,
                    },
                    "carbon_guard": {
                        "objective": "minimize_worst_scenario_emissions",
                        "max_worst_emissions": 78,
                    },
                },
            ),
            (
                "contracts",
                {
                    "cost_guard": {
                        "objective": "minimize_worst_scenario_cost",
                        "max_worst_cost": -1,
                    },
                    "carbon_guard": {
                        "objective": "minimize_worst_scenario_emissions",
                        "max_worst_emissions": 78,
                    },
                },
            ),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                data = copy.deepcopy(self.data)
                data[key] = value
                with self.assertRaises(ValueError):
                    validate_intake(data)

    def test_too_many_periods_and_scenarios_are_rejected(self):
        data = copy.deepcopy(self.data)
        for key in ("grid_cost", "diesel_cost", "grid_emissions", "diesel_emissions"):
            data[key] = data[key] + [1, 1]
        for scenario in data["scenarios"].values():
            scenario["base_load"].extend([0, 0])
            scenario["grid_max"].extend([1, 1])
        with self.assertRaises(ValueError):
            validate_intake(data)

        data = copy.deepcopy(self.data)
        data["scenarios"]["spare"] = copy.deepcopy(data["scenarios"]["normal"])
        data["scenarios"]["spare2"] = copy.deepcopy(data["scenarios"]["normal"])
        with self.assertRaises(ValueError):
            validate_intake(data)


class AuditPowerScenarioTests(unittest.TestCase):
    def setUp(self):
        self.data = load_fixture()
        self.result = audit()

    def test_outage_limits_change_the_plan_and_can_reject(self):
        tightened = copy.deepcopy(self.data)
        tightened["scenarios"]["outage"]["grid_max"] = [4, 1, 2]
        report = optimize_intake(tightened, self.result)
        self.assertEqual(report["cost_guard"]["worst_cost"], 67)
        self.assertEqual(report["carbon_guard"]["worst_emissions"], 84)
        self.assertEqual(report["cost_guard"]["status"], "rejected")
        self.assertEqual(report["carbon_guard"]["status"], "rejected")
        self.assertNotEqual(
            report["certificate"]["minimum_worst_emissions"],
            optimize_intake(self.data, self.result)["certificate"][
                "minimum_worst_emissions"
            ],
        )
        self.assertNotEqual(
            report["certificate"]["all_first_stage_exact_suffix_bounds"],
            optimize_intake(self.data, self.result)["certificate"][
                "all_first_stage_exact_suffix_bounds"
            ],
        )

        # A looser outage cap allows cheaper repair and signs both contracts.
        loosened = copy.deepcopy(self.data)
        loosened["scenarios"]["outage"]["grid_max"] = [4, 2, 3]
        report = optimize_intake(loosened, self.result)
        self.assertEqual(report["cost_guard"]["worst_cost"], 57)
        self.assertEqual(report["carbon_guard"]["worst_emissions"], 71)
        self.assertEqual(report["cost_guard"]["status"], "signed")
        self.assertEqual(report["carbon_guard"]["status"], "signed")

    def test_final_reserve_infeasibility_rejects_both_contracts(self):
        data = copy.deepcopy(self.data)
        data["battery"]["final_reserve"] = 4  # capacity is only 3
        report = optimize_intake(data, self.result)
        for name in ("cost_guard", "carbon_guard"):
            contract = report[name]
            self.assertEqual(contract["status"], "rejected")
            self.assertFalse(contract["signed"])
            self.assertIsNone(contract["common_period_1"])
            self.assertIn("final reserve", contract["reason"])
        self.assertEqual(report["certificate"]["feasible_first_decisions"], [])
        self.assertIsNone(report["certificate"]["minimum_worst_cost"])

    def test_impossible_outage_demand_rejects_contracts(self):
        data = copy.deepcopy(self.data)
        data["scenarios"]["outage"]["base_load"] = [2, 4, 9]
        data["scenarios"]["outage"]["grid_max"] = [4, 1, 3]
        report = optimize_intake(data, self.result)
        self.assertEqual(report["cost_guard"]["status"], "rejected")
        self.assertEqual(report["carbon_guard"]["status"], "rejected")
        self.assertIn("full horizon", report["cost_guard"]["reason"])
        self.assertTrue(
            any(
                "outage" in row["reason"]
                for row in report["certificate"]["infeasible_first_decisions"]
            )
        )

    def test_contract_limit_rejection_keeps_the_optimal_plan(self):
        data = copy.deepcopy(self.data)
        data["contracts"]["cost_guard"]["max_worst_cost"] = 62
        report = optimize_intake(data, self.result)
        contract = report["cost_guard"]
        self.assertEqual(contract["common_period_1"], {"grid": 4, "diesel": 2})
        self.assertEqual(contract["worst_cost"], 63)
        self.assertEqual(contract["status"], "rejected")
        self.assertIn("exceeds", contract["reason"])
        # Only cost_guard is affected; carbon_guard still signs.
        self.assertEqual(report["carbon_guard"]["status"], "signed")

    def test_ties_break_on_first_grid_then_diesel(self):
        data = copy.deepcopy(self.data)
        # Make every first decision equally cheap so only the tie-break orders
        # the contracts.  Grid and diesel costs are all zero, so both guards
        # chase the smallest purchase that still meets the horizon.
        for key in ("grid_cost", "diesel_cost"):
            data[key] = [0, 0, 0]
        data["contracts"]["cost_guard"]["max_worst_cost"] = 0
        report = optimize_intake(data, self.result)
        contract = report["cost_guard"]
        self.assertEqual(contract["worst_cost"], 0)
        feasible = report["certificate"]["feasible_first_decisions"]
        best = min(feasible, key=lambda row: (row["grid"], row["diesel"]))
        self.assertEqual(
            contract["common_period_1"],
            {"grid": best["grid"], "diesel": best["diesel"]},
        )

    def test_four_periods_and_three_scenarios_are_supported(self):
        data = {
            "snapshot_id": self.data["snapshot_id"],
            "run_key": self.data["run_key"],
            "energy_per_audited_row": 1,
            "battery": {"initial": 1, "capacity": 3, "final_reserve": 1},
            "diesel_max_each_period": 3,
            "grid_cost": [2, 2, 3, 2],
            "diesel_cost": [7, 8, 7, 7],
            "grid_emissions": [5, 4, 3, 5],
            "diesel_emissions": [4, 9, 10, 4],
            "scenarios": {
                "normal": {"base_load": [2, 1, 2, 1], "grid_max": [4, 4, 5, 4]},
                "outage": {"base_load": [2, 4, 2, 2], "grid_max": [4, 1, 3, 3]},
                "sag": {"base_load": [2, 2, 3, 2], "grid_max": [4, 2, 2, 3]},
            },
            "revealed_after_period": 1,
            "contracts": {
                "cost_guard": {
                    "objective": "minimize_worst_scenario_cost",
                    "max_worst_cost": 200,
                },
                "carbon_guard": {
                    "objective": "minimize_worst_scenario_emissions",
                    "max_worst_emissions": 200,
                },
            },
        }
        self.assertIs(
            validate_intake(data)["periods"],
            4,
        )
        report = optimize_intake(data, self.result)
        self.assertEqual(report["certificate"]["periods"], 4)
        self.assertEqual(
            report["certificate"]["scenario_order"], ["normal", "outage", "sag"]
        )
        for scenario in ("normal", "outage", "sag"):
            self.assertEqual(
                len(report["cost_guard"]["scenarios"][scenario]["dispatch"]), 4
            )
        self.assertEqual(report["cost_guard"]["status"], "signed")
        self.assertEqual(report["carbon_guard"]["status"], "signed")

    def test_single_period_fixture_is_supported(self):
        data = {
            "snapshot_id": self.data["snapshot_id"],
            "run_key": self.data["run_key"],
            "energy_per_audited_row": 1,
            "battery": {"initial": 1, "capacity": 3, "final_reserve": 1},
            "diesel_max_each_period": 3,
            "grid_cost": [2],
            "diesel_cost": [7],
            "grid_emissions": [5],
            "diesel_emissions": [4],
            "scenarios": {
                "normal": {"base_load": [2], "grid_max": [4]},
                "outage": {"base_load": [2], "grid_max": [4]},
            },
            "revealed_after_period": 1,
            "contracts": {
                "cost_guard": {
                    "objective": "minimize_worst_scenario_cost",
                    "max_worst_cost": 65,
                },
                "carbon_guard": {
                    "objective": "minimize_worst_scenario_emissions",
                    "max_worst_emissions": 78,
                },
            },
        }
        report = optimize_intake(data, self.result)
        self.assertEqual(report["certificate"]["periods"], 1)
        self.assertEqual(len(report["cost_guard"]["scenarios"]["normal"]["dispatch"]), 1)
        self.assertGreaterEqual(
            report["cost_guard"]["scenarios"]["normal"]["battery"][-1],
            data["battery"]["final_reserve"],
        )

    def test_audited_rows_change_recomputes_everything(self):
        """Adding a row to the source lake must change demand and decisions."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "lake"
            shutil.copytree(LAKE, workspace)
            (workspace / "data/base.csv").write_text(
                "_row_id,_row_pos,category,region,amount,event_date\n"
                "R0001,0,alpha,north,100,2024-01-01\n"
                "R0002,1,beta,south,200,2024-01-02\n"
                "R0003,2,gamma,east,300,2024-01-03\n",
                encoding="utf-8",
            )
            (workspace / "expected/visible.csv").write_text(
                "file_id,row_pos,_row_id,expected_visible\n"
                "base,0,R0001,true\n"
                "base,1,R0002,true\n"
                "base,2,R0003,true\n",
                encoding="utf-8",
            )
            snapshot_path = workspace / "snapshots/snap_001.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["data_files"][0]["row_count"] = 3
            snapshot["data_files"][0]["sha256"] = hashlib.sha256(
                (workspace / "data/base.csv").read_bytes()
            ).hexdigest()
            snapshot_path.write_text(
                json.dumps(snapshot, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            result = execute_audit(workspace)

        self.assertEqual(result.stats["rows"], 3)
        self.assertEqual(result.stats["diffs"], 0)
        self.assertNotEqual(result.run_key, self.data["run_key"])
        data = copy.deepcopy(self.data)
        data["run_key"] = result.run_key
        report = optimize_intake(data, result)
        self.assertEqual(report["audited_rows"], 3)
        self.assertEqual(
            report["certificate"]["demand"],
            {"normal": [5, 4, 5], "outage": [5, 7, 5]},
        )
        self.assertNotEqual(
            report["cost_guard"]["common_period_1"], {"grid": 4, "diesel": 2}
        )
        self.assertEqual(report["cost_guard"]["worst_cost"], 85)
        self.assertEqual(report["carbon_guard"]["worst_emissions"], 102)
        self.assertEqual(report["cost_guard"]["status"], "rejected")
        self.assertEqual(report["carbon_guard"]["status"], "rejected")
        # The old run_key no longer binds to the changed lake.
        with self.assertRaises(ValueError):
            optimize_intake(self.data, result)


class AuditPowerCliTests(unittest.TestCase):
    def test_cli_prints_the_signed_plan(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(["--fixture", str(FIXTURE)])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["audited_rows"], 2)
        self.assertEqual(payload["cost_guard"]["worst_cost"], 63)
        self.assertEqual(payload["carbon_guard"]["worst_emissions"], 77)
        self.assertEqual(len(payload["certificate"]["feasible_first_decisions"]), 5)

    def test_cli_reports_errors_with_exit_code_three(self):
        import contextlib
        import io
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{}", encoding="utf-8")
            buffer = io.StringIO()
            with contextlib.redirect_stderr(buffer):
                code = main(["--fixture", str(bad)])
        self.assertEqual(code, 3)
        self.assertIn("error:", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
