import sqlite3
import tempfile
import unittest
from pathlib import Path

from support import LAKE

from lakedel.errors import AuditError
from lakedel.ledger import verify_ledger
from lakedel.service import produce_outputs


class LedgerTests(unittest.TestCase):
    def test_write_verify_and_idempotent_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "audit"
            db = out / "ledger.sqlite"
            result, _, _ = produce_outputs(LAKE, out, ["json", "md"], db)
            verification = verify_ledger(db)
            self.assertTrue(verification["ok"], verification)
            self.assertEqual(verification["runs"], 1)
            produce_outputs(LAKE, out, ["json", "md"], db)
            verification = verify_ledger(db)
            self.assertTrue(verification["ok"], verification)
            connection = sqlite3.connect(str(db))
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM row_visibility").fetchone()[0],
                    14,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM findings").fetchone()[0],
                    8,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 2
                )
                self.assertEqual(result.stats["diffs"], 0)
            finally:
                connection.close()

    def test_artifact_hash_tamper_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "audit"
            db = out / "ledger.sqlite"
            produce_outputs(LAKE, out, ["json", "md"], db)
            report = out / "report.md"
            report.write_text(report.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")
            verification = verify_ledger(db)
            self.assertFalse(verification["ok"])
            failed = [item for item in verification["checks"] if not item["ok"]]
            self.assertTrue(any("artifact:report.md" in item["name"] for item in failed))

    def test_missing_ledger(self):
        with self.assertRaises(AuditError) as ctx:
            verify_ledger(Path("does-not-exist.sqlite"))
        self.assertEqual(ctx.exception.code, "LEDGER_CHECK_FAILED")

    def test_rerun_with_new_out_dir_records_same_run_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = produce_outputs(LAKE, Path(tmp) / "a", ["json"], Path(tmp) / "a" / "x.sqlite")
            second = produce_outputs(LAKE, Path(tmp) / "b", ["json"], Path(tmp) / "b" / "x.sqlite")
            self.assertEqual(first[0].run_key, second[0].run_key)
            self.assertEqual(verify_ledger(Path(tmp) / "b" / "x.sqlite")["runs"], 1)


if __name__ == "__main__":
    unittest.main()
