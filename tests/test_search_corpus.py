import json
import unittest
from pathlib import Path

from lakedel.search_corpus import inspect_search_corpus


ROOT = Path(__file__).resolve().parents[1]


class SearchCorpusTest(unittest.TestCase):
    def test_visible_corpus_uses_real_delete_audit(self):
        result = inspect_search_corpus(ROOT / "examples/lake",
                                       ROOT / "examples/lake/search_catalog.json")
        self.assertEqual(result["visible_count"], 8)
        self.assertNotIn("R0001", result["visible_document_ids"])
        self.assertIn("R0008", result["visible_document_ids"])
        self.assertEqual(result["term_document_frequency"]["repair"], 5)
        self.assertIsNone(result["top_k"])

    def test_catalog_must_match_audited_rows(self):
        path = ROOT / "examples/lake/search_catalog.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["documents"][0]["region"] = "south"
        with self.subTest("bad region"):
            import tempfile
            with tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / "catalog.json"
                target.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "catalog region"):
                    inspect_search_corpus(ROOT / "examples/lake", target)
