import json
import tempfile
import unittest
from pathlib import Path

from lakedel.cohort_evidence import inspect_sample


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "examples" / "lake"
MANIFEST = ROOT / "examples" / "quality_sample" / "manifest.json"


class CohortEvidenceTest(unittest.TestCase):
    def test_visible_sample_and_stable_evidence(self):
        result = inspect_sample(WORKSPACE, MANIFEST)
        self.assertEqual(result["population_by_region"], {"east": 2, "north": 1, "south": 2, "west": 3})
        self.assertEqual(result["sample_by_region"], {"east": 1, "north": 1, "south": 1, "west": 2})
        self.assertEqual(result["sample_defects"], 1)
        self.assertIsNone(result["publication_decision"])
        self.assertEqual(result, inspect_sample(WORKSPACE, MANIFEST))

    def test_hidden_or_duplicate_inspection_rejected(self):
        base = json.loads(MANIFEST.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.json"
            base["inspections"][0]["row_id"] = "R0001"
            path.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not auditable and visible"):
                inspect_sample(WORKSPACE, path)
            base["inspections"][0]["row_id"] = "R0007"
            path.write_text(json.dumps(base), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicated"):
                inspect_sample(WORKSPACE, path)


if __name__ == "__main__":
    unittest.main()
