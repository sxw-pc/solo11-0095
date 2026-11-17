import tempfile
import unittest
from collections import Counter
from pathlib import Path

from support import LAKE, read_json

from lakedel.delete_vectors import load_delete_vectors
from lakedel.engine import execute_audit
from lakedel.models import DataFileSpec, DeleteFileSpec, RowRecord
from lakedel.schema import parse_schema
from lakedel.util import sha256_bytes


class DeleteVectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = execute_audit(LAKE)
        cls.ops = cls.result.operations
        cls.codes = Counter(item.code for item in cls.result.findings)

    def test_operation_and_applicable_counts(self):
        self.assertEqual(len(self.ops), 10)
        self.assertEqual(sum(1 for item in self.ops if item.applicable), 3)

    def test_all_conflict_classes_are_reported_once(self):
        expected = {
            "STALE_SEQUENCE": 1,
            "DUPLICATE_POSITION_DELETE": 1,
            "OUT_OF_RANGE_POSITION": 1,
            "ORPHAN_DELETE_TARGET": 1,
            "DUPLICATE_EQUALITY_DELETE": 1,
            "EQUALITY_NO_MATCH": 1,
            "DELETE_SCHEMA_MISMATCH": 1,
            "AMBIGUOUS_EQUALITY_COLUMNS": 1,
        }
        self.assertEqual(dict(self.codes), expected)
        self.assertNotIn("DELETE_ENTRY_INVALID", self.codes)
        self.assertEqual(len(self.result.findings), 8)

    def test_stale_position_file_is_not_applied(self):
        stale = [item for item in self.ops if item.delete_file_id == "pos_011"]
        self.assertEqual(len(stale), 4)
        self.assertFalse(any(item.applicable for item in stale))
        self.assertEqual(
            {item.conflict_code for item in stale},
            {
                "STALE_SEQUENCE",
                "DUPLICATE_POSITION_DELETE",
                "OUT_OF_RANGE_POSITION",
                "ORPHAN_DELETE_TARGET",
            },
        )

    def test_equality_duplicate_and_no_match(self):
        equality = [
            item for item in self.ops if item.delete_file_id == "eq_020"
        ]
        self.assertEqual(len(equality), 3)
        self.assertTrue(equality[0].applicable)
        self.assertEqual(equality[1].conflict_code, "DUPLICATE_EQUALITY_DELETE")
        self.assertEqual(equality[2].conflict_code, "EQUALITY_NO_MATCH")
        self.assertEqual(equality[0].key, {"region": "north"})

    def test_schema_mismatch_and_ambiguous_file_is_not_applied(self):
        ops = [item for item in self.ops if item.delete_file_id == "eq_021"]
        self.assertEqual(len(ops), 1)
        self.assertFalse(ops[0].applicable)
        self.assertIn(
            ops[0].conflict_code,
            {"AMBIGUOUS_EQUALITY_COLUMNS", "DELETE_SCHEMA_MISMATCH"},
        )
        self.assertEqual(self.codes["DELETE_SCHEMA_MISMATCH"], 1)
        self.assertEqual(self.codes["AMBIGUOUS_EQUALITY_COLUMNS"], 1)

    def test_operations_keep_audit_reasons(self):
        self.assertTrue(all(item.reason.startswith("fixture:") for item in self.ops))

    def test_stale_equality_delete_with_target_is_not_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deletes").mkdir()
            content = '{"key":{"region":"north"},"reason":"stale equality"}\n'
            (root / "deletes" / "eq.jsonl").write_bytes(content.encode("utf-8"))
            schema = parse_schema(read_json(LAKE / "table.json")["schema"])
            data_spec = DataFileSpec(
                "f", "data/f.csv", "csv", 1, "0" * 64, 5, schema.schema_hash
            )
            delete_spec = DeleteFileSpec(
                "eq", "deletes/eq.jsonl", "equality", 3, schema.schema_hash,
                sha256_bytes(content.encode("utf-8")), "f", ("region",),
            )
            rows = (RowRecord("f", 0, "R1", {"region": "north"}),)
            ops, findings = load_delete_vectors(
                root, (delete_spec,), schema, (data_spec,), rows
            )
            self.assertEqual([item.code for item in findings], ["STALE_SEQUENCE"])
            self.assertFalse(ops[0].applicable)
            self.assertEqual(ops[0].conflict_code, "STALE_SEQUENCE")


if __name__ == "__main__":
    unittest.main()
