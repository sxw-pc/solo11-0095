import copy
import unittest

from support import LAKE, read_json

from lakedel.errors import AuditError
from lakedel.manifest import parse_snapshot, parse_table


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.table_raw = read_json(LAKE / "table.json")
        self.snapshot_raw = read_json(LAKE / "snapshots" / "snap_001.json")
        self.table = parse_table(copy.deepcopy(self.table_raw))

    def test_table_schema_and_metadata(self):
        self.assertEqual(self.table.table_id, "tbl_demo_001")
        self.assertEqual(self.table.schema.schema_hash, self.snapshot_raw["schema_hash"])
        self.assertEqual(self.table.schema.names[-1], "event_date")
        self.assertEqual(len(self.table.schema.columns), 6)

    def test_table_missing_field(self):
        raw = copy.deepcopy(self.table_raw)
        del raw["table_id"]
        with self.assertRaises(AuditError) as ctx:
            parse_table(raw)
        self.assertEqual(ctx.exception.code, "MANIFEST_MISSING_FIELD")

    def test_table_bad_format_version(self):
        raw = copy.deepcopy(self.table_raw)
        raw["format_version"] = 2
        with self.assertRaises(AuditError) as ctx:
            parse_table(raw)
        self.assertEqual(ctx.exception.code, "MANIFEST_TYPE_ERROR")

    def test_snapshot_parses_files(self):
        findings = []
        snapshot = parse_snapshot(
            copy.deepcopy(self.snapshot_raw),
            "snapshots/snap_001.json",
            self.table.schema.schema_hash,
            findings,
        )
        self.assertEqual(len(snapshot.data_files), 3)
        self.assertEqual(len(snapshot.delete_files), 4)
        self.assertEqual(snapshot.delete_files[0].equality_columns, ())
        self.assertEqual(snapshot.delete_files[2].equality_columns, ("region",))
        self.assertEqual(findings, [])

    def test_snapshot_schema_mismatch_finding(self):
        findings = []
        parse_snapshot(
            copy.deepcopy(self.snapshot_raw),
            "snapshots/snap_001.json",
            "f" * 64,
            findings,
        )
        self.assertEqual([item.code for item in findings], ["SCHEMA_HASH_MISMATCH"])

    def test_unknown_data_format_skipped(self):
        raw = copy.deepcopy(self.snapshot_raw)
        raw["data_files"][0]["format"] = "parquet"
        findings = []
        snapshot = parse_snapshot(raw, "snap", self.table.schema.schema_hash, findings)
        self.assertEqual(len(snapshot.data_files), 2)
        self.assertEqual([item.code for item in findings], ["UNKNOWN_FORMAT"])

    def test_duplicate_data_id_skipped(self):
        raw = copy.deepcopy(self.snapshot_raw)
        raw["data_files"].append(copy.deepcopy(raw["data_files"][0]))
        findings = []
        snapshot = parse_snapshot(raw, "snap", self.table.schema.schema_hash, findings)
        self.assertEqual(len(snapshot.data_files), 3)
        self.assertIn("DUPLICATE_ID", [item.code for item in findings])

    def test_bad_row_count_type(self):
        raw = copy.deepcopy(self.snapshot_raw)
        raw["data_files"][0]["row_count"] = True
        findings = []
        snapshot = parse_snapshot(raw, "snap", self.table.schema.schema_hash, findings)
        self.assertEqual(len(snapshot.data_files), 2)
        self.assertEqual(findings[0].code, "MANIFEST_TYPE_ERROR")

    def test_snapshot_top_level_must_be_object(self):
        with self.assertRaises(AuditError) as ctx:
            parse_snapshot([], "snap", "x", [])
        self.assertEqual(ctx.exception.code, "MANIFEST_TYPE_ERROR")


if __name__ == "__main__":
    unittest.main()
