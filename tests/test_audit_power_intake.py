import json
from pathlib import Path
import unittest

from lakedel.audit_power_intake import normal_preview
from lakedel.engine import execute_audit


ROOT = Path(__file__).resolve().parents[1]


class AuditPowerIntakeTests(unittest.TestCase):
    def test_preview_uses_real_audit_row_count_but_ignores_outage(self):
        data = json.loads((ROOT / "examples/audit_power/intake.json").read_text(encoding="utf-8"))
        result = execute_audit(ROOT / "examples/audit_power/lake")
        report = normal_preview(data, result)
        self.assertEqual(report["audited_rows"], 2)
        self.assertEqual(report["normal_demand"], [4, 3, 4])
        self.assertEqual(report["normal_cost"], 26)
        self.assertGreater(6, data["scenarios"]["outage"]["grid_max"][1] + data["diesel_max_each_period"])

    def test_witness_dispatches_balance_battery_and_objectives(self):
        data = json.loads((ROOT / "examples/audit_power/intake.json").read_text(encoding="utf-8"))
        witness = json.loads((ROOT / "examples/audit_power/witness.json").read_text(encoding="utf-8"))
        for plan in (witness["cost_guard"], witness["carbon_guard"]):
            costs = []
            emissions = []
            for name, scenario in data["scenarios"].items():
                dispatch = [plan["common_period_1"], *plan[f"{name}_later"]]
                charge = data["battery"]["initial"]
                cost = emission = 0
                for t, row in enumerate(dispatch):
                    demand = scenario["base_load"][t] + 2 * data["energy_per_audited_row"]
                    self.assertLessEqual(row["grid"], scenario["grid_max"][t])
                    self.assertLessEqual(row["diesel"], data["diesel_max_each_period"])
                    charge += row["grid"] + row["diesel"] - demand
                    self.assertGreaterEqual(charge, 0)
                    self.assertLessEqual(charge, data["battery"]["capacity"])
                    cost += row["grid"] * data["grid_cost"][t] + row["diesel"] * data["diesel_cost"][t]
                    emission += row["grid"] * data["grid_emissions"][t] + row["diesel"] * data["diesel_emissions"][t]
                self.assertGreaterEqual(charge, data["battery"]["final_reserve"])
                costs.append(cost)
                emissions.append(emission)
            self.assertEqual(max(costs), plan["worst_cost"])
            self.assertEqual(max(emissions), plan["worst_emissions"])
