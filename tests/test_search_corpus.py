import json
import unittest
from pathlib import Path

from lakedel.search_corpus import inspect_search_corpus
from lakedel.search_blocks import BlockQueryStore, inspect_search_blocks


ROOT = Path(__file__).resolve().parents[1]


class SearchCorpusTest(unittest.TestCase):
    def test_large_index_is_valid_but_has_no_rankings(self):
        result = inspect_search_blocks(ROOT / "examples/search_lake")
        self.assertEqual(result["visible_count"], 992)
        self.assertEqual(result["visible_term_df"]["repair"], 1)
        self.assertEqual(result["maximum_block_fetches"], 2)
        self.assertIsNone(result["rankings"])

    def test_query_store_enforces_two_block_reads(self):
        workspace = ROOT / "examples/search_lake"
        store = BlockQueryStore(workspace, 2)
        self.assertTrue(store.blocks("repair"))
        self.assertTrue(store.fetch("repair", 31))
        self.assertTrue(store.fetch("audit", 31))
        self.assertEqual(store.block_fetches, 1)
        self.assertTrue(store.fetch("audit", 21))
        with self.assertRaisesRegex(ValueError, "budget exceeded"):
            store.fetch("audit", 20)

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
