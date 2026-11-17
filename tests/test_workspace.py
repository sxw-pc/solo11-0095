import os
import tempfile
import unittest
from pathlib import Path

from support import LAKE, copy_lake, read_json, write_json

from lakedel.errors import AuditError
from lakedel.util import safe_join
from lakedel.workspace import load_workspace


class WorkspaceTests(unittest.TestCase):
    def test_loads_fixture_deterministically(self):
        first = load_workspace(LAKE)
        second = load_workspace(LAKE)
        self.assertEqual(first.snapshot.snapshot_id, "snap_001")
        self.assertEqual(first.snapshot.sequence_number, 5)
        self.assertEqual(len(first.rows), 14)
        self.assertEqual(len(first.expected), 14)
        self.assertEqual(len(first.findings), 0)
        self.assertEqual(len(first.fingerprint), 64)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(
            [(row.file_id, row.row_pos) for row in first.rows],
            sorted((row.file_id, row.row_pos) for row in first.rows),
        )

    def test_explicit_snapshot(self):
        data = load_workspace(LAKE, "snap_001")
        self.assertEqual(data.snapshot.snapshot_id, "snap_001")
        with self.assertRaises(AuditError) as ctx:
            load_workspace(LAKE, "snap_missing")
        self.assertEqual(ctx.exception.code, "SNAPSHOT_NOT_FOUND")

    def test_invalid_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AuditError) as ctx:
                load_workspace(Path(tmp) / "missing")
            self.assertEqual(ctx.exception.code, "WORKSPACE_INVALID")

    def test_traversal_manifest_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake = copy_lake(Path(tmp) / "lake")
            snapshot_path = lake / "snapshots" / "snap_001.json"
            snapshot = read_json(snapshot_path)
            snapshot["data_files"][0]["path"] = "../outside.csv"
            write_json(snapshot_path, snapshot)
            with self.assertRaises(AuditError) as ctx:
                load_workspace(lake)
            self.assertEqual(ctx.exception.code, "PATH_ESCAPE")

    def test_absolute_manifest_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake = copy_lake(Path(tmp) / "lake")
            snapshot_path = lake / "snapshots" / "snap_001.json"
            snapshot = read_json(snapshot_path)
            snapshot["delete_files"][0]["path"] = "C:/secret.jsonl"
            write_json(snapshot_path, snapshot)
            with self.assertRaises(AuditError) as ctx:
                load_workspace(lake)
            self.assertEqual(ctx.exception.code, "PATH_ESCAPE")

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            lake = copy_lake(base / "lake")
            outside = base / "outside.csv"
            outside.write_text("_row_id,_row_pos\nx,0\n", encoding="utf-8")
            link = lake / "data" / "escape.csv"
            try:
                os.symlink(outside, link)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            snapshot_path = lake / "snapshots" / "snap_001.json"
            snapshot = read_json(snapshot_path)
            snapshot["data_files"][0]["path"] = "data/escape.csv"
            write_json(snapshot_path, snapshot)
            with self.assertRaises(AuditError) as ctx:
                load_workspace(lake)
            self.assertEqual(ctx.exception.code, "PATH_ESCAPE")

    def test_safe_join_rejects_unsafe_forms(self):
        for value in ("../x", "/abs/x", "C:/x", "a\\b", "a//b", "a/./b", ""):
            with self.subTest(value=value):
                with self.assertRaises(AuditError) as ctx:
                    safe_join(LAKE, value)
                self.assertEqual(ctx.exception.code, "PATH_ESCAPE")

    def test_safe_join_accepts_normal_relative_path(self):
        result = safe_join(LAKE, "data/base_001.csv")
        self.assertTrue(result.is_file())
        self.assertEqual(result, (LAKE / "data" / "base_001.csv").resolve())


if __name__ == "__main__":
    unittest.main()
