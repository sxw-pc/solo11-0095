import tempfile
import unittest
from pathlib import Path

from lakedel.errors import Finding
from lakedel.models import ExpectedRow, RowDecision, RowRecord
from lakedel.reconcile import load_expected, reconcile


def decision(pos, visible):
    reason = "VISIBLE_NO_DELETE" if visible else "DELETED_BY_POSITION"
    return RowDecision("f", pos, f"R{pos}", visible, reason)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.rows = tuple(
            RowRecord("f", pos, f"R{pos}", {"_row_pos": pos}) for pos in range(4)
        )
        self.decisions = (
            decision(0, True),
            decision(1, False),
            decision(2, True),
            decision(3, True),
        )
        self.expected = (
            ExpectedRow("f", 0, "R0", True),
            ExpectedRow("f", 1, "R1", True),
            ExpectedRow("f", 2, "R2", False),
            ExpectedRow("g", 0, "R9", True),
        )

    def test_all_four_classifications(self):
        findings = [Finding("STALE_SEQUENCE", "stale", "f", 1)]
        diffs, summary = reconcile(self.rows, self.decisions, self.expected, findings)
        self.assertEqual(
            [(item.file_id, item.row_pos, item.classification) for item in diffs],
            [
                ("f", 1, "MISSING_VISIBLE"),
                ("f", 2, "UNEXPECTED_VISIBLE"),
                ("f", 3, "MISSING_EXPECTED_ROW"),
                ("g", 0, "EXTRA_EXPECTED_ROW"),
            ],
        )
        self.assertEqual(
            summary,
            {
                "MISSING_VISIBLE": 1,
                "UNEXPECTED_VISIBLE": 1,
                "EXTRA_EXPECTED_ROW": 1,
                "MISSING_EXPECTED_ROW": 1,
                "total": 4,
            },
        )
        self.assertEqual(diffs[0].first_finding, "STALE_SEQUENCE")
        self.assertEqual(
            sorted(item.code for item in findings if item.code in summary),
            sorted(code for code in summary if code != "total"),
        )

    def test_load_expected_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "expected.csv"
            path.write_text(
                "file_id,row_pos,_row_id,expected_visible\n"
                "f,1,R1,false\n"
                "f,0,R0,TRUE\n",
                encoding="utf-8",
            )
            findings = []
            expected = load_expected(path, findings)
            self.assertEqual(findings, [])
            self.assertEqual(
                [(item.file_id, item.row_pos, item.visible) for item in expected],
                [("f", 0, True), ("f", 1, False)],
            )

    def test_load_expected_reports_bad_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "expected.csv"
            path.write_text(
                "file_id,row_pos,expected_visible\n"
                "f,0,true\n"
                "\n"
                "f,0,false\n"
                "f,-1,true\n"
                "f,2,maybe\n",
                encoding="utf-8",
            )
            findings = []
            expected = load_expected(path, findings)
            codes = [item.code for item in findings]
            self.assertIn("BLANK_LINE", codes)
            self.assertIn("DUPLICATE_EXPECTED_ROW", codes)
            self.assertGreaterEqual(codes.count("EXPECTED_INVALID"), 2)
            self.assertEqual(len(expected), 1)

    def test_load_expected_missing_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "expected.csv"
            path.write_text("file_id,row_pos\nf,0\n", encoding="utf-8")
            findings = []
            self.assertEqual(load_expected(path, findings), ())
            self.assertEqual(findings[0].code, "MISSING_COLUMN")


if __name__ == "__main__":
    unittest.main()
