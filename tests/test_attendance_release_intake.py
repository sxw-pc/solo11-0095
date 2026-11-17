import json
from pathlib import Path
import unittest

from lakedel.attendance_release_intake import naive_preview
from lakedel.engine import execute_audit


ROOT = Path(__file__).resolve().parents[1]


class AttendanceReleaseIntakeTests(unittest.TestCase):
    def test_delete_aware_preview_still_leaks_masked_small_cell(self):
        data = json.loads((ROOT / "examples/attendance_release/intake.json").read_text(encoding="utf-8"))
        result = execute_audit(ROOT / "examples/attendance_release/lake")
        self.assertEqual(result.stats["visible_rows"], 32)
        self.assertEqual(result.stats["hidden_rows"], 1)
        self.assertEqual(result.stats["diffs"], 0)
        report = naive_preview(data, result)
        self.assertEqual(report["masked_cells"], ["local:morning"])
        self.assertEqual(report["row_totals"], {"local": 13, "visiting": 19})
        self.assertEqual(report["column_totals"], {"morning": 9, "afternoon": 10, "evening": 13})
        self.assertEqual(report["row_totals"]["local"] - report["visible_cells"]["local:afternoon"]
                         - report["visible_cells"]["local:evening"], 2)
        self.assertEqual(report["column_totals"]["morning"] - report["visible_cells"]["visiting:morning"], 2)

    def test_alternative_grids_preserve_all_margins(self):
        witness = json.loads((ROOT / "examples/attendance_release/witness.json").read_text(encoding="utf-8"))
        grids = [witness["audited_visible_grid"], *witness["alternative_grids"]]
        self.assertEqual({g["local"]["morning"] for g in grids}, {1, 2, 3})
        for grid in grids:
            self.assertEqual([sum(grid[row].values()) for row in ["local", "visiting"]], [13, 19])
            self.assertEqual([grid["local"][col] + grid["visiting"][col]
                              for col in ["morning", "afternoon", "evening"]], [9, 10, 13])
