import json
import tempfile
import unittest
from pathlib import Path

from lakedel.cohort_transfer import inspect_deletion_cohort


ROOT = Path(__file__).resolve().parents[1] / "examples" / "quality_transfer"
DRAW = ROOT / "draw.json"


class CohortTransferTest(unittest.TestCase):
    def test_verified_post_draw_deletion_evidence(self):
        result = inspect_deletion_cohort(ROOT, DRAW)
        self.assertEqual(result["population_before_by_region"], {"east": 20, "west": 20})
        self.assertEqual(result["population_after_by_region"], {"east": 15, "west": 20})
        self.assertEqual(result["sample_by_region"], {"east": 10, "west": 10})
        self.assertEqual(result["deleted_inspected_count"], 0)
        self.assertEqual(result["surviving_inspected_count"], 20)
        self.assertEqual(result["sample_defects_by_region"], {})
        self.assertIsNone(result["population_upper_bounds"])
        self.assertIsNone(result["publication_decision"])
        self.assertEqual(result, inspect_deletion_cohort(ROOT, DRAW))

    def test_changed_draw_or_snapshot_binding_rejected(self):
        draw = json.loads(DRAW.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draw.json"
            draw["inspections"][0]["row_id"] = "Q0001"
            path.write_text(json.dumps(draw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fixed random draw"):
                inspect_deletion_cohort(ROOT, path)
            draw = json.loads(DRAW.read_text(encoding="utf-8"))
            draw["after_snapshot"] = "wrong"
            path.write_text(json.dumps(draw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "snapshot binding"):
                inspect_deletion_cohort(ROOT, path)


if __name__ == "__main__":
    unittest.main()
