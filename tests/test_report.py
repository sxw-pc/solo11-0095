import json
import tempfile
import unittest
from pathlib import Path

from support import LAKE, copy_lake

from lakedel.engine import execute_audit
from lakedel.report import build_report, render_html, render_markdown, write_reports


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = execute_audit(LAKE)

    def test_json_report_shape(self):
        report = build_report(self.result, "out/demo/ledger.sqlite")
        self.assertEqual(report["project"]["name"], "行删镜")
        self.assertEqual(report["project"]["rule_version"], "MOR-1.0")
        self.assertEqual(report["snapshot"]["snapshot_id"], "snap_001")
        self.assertEqual(report["summary"]["rows"], 14)
        self.assertEqual(report["summary"]["visible_rows"], 8)
        self.assertEqual(report["diff_summary"]["total"], 0)
        self.assertEqual(len(report["findings"]), 8)
        self.assertEqual(report["ledger_path"], "out/demo/ledger.sqlite")
        self.assertEqual(
            [item["ordinal"] for item in report["findings"]], list(range(1, 9))
        )
        json.dumps(report, ensure_ascii=False, sort_keys=True)

    def test_markdown_sections(self):
        report = build_report(self.result, "out/demo/ledger.sqlite")
        text = render_markdown(report)
        for heading in ("## 结论", "## 统计", "## 问题分类", "## 差异样例", "## 限制说明"):
            self.assertIn(heading, text)
        self.assertIn("未发现差异。", text)
        self.assertIn("DELETED_BY_EQUALITY", text)
        self.assertTrue(text.endswith("\n"))

    def test_html_is_self_contained(self):
        report = build_report(self.result, "out/demo/ledger.sqlite")
        text = render_html(report)
        for marker in ('id="summary"', 'id="findings"', 'id="diffs"'):
            self.assertIn(marker, text)
        self.assertIn("<style>", text)
        for forbidden in ("http://", "https://", "<script", "<link", "@import", "url("):
            self.assertNotIn(forbidden, text)
        self.assertIn("本报告完全离线生成", text)

    def test_markdown_diff_row_has_all_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake = copy_lake(Path(tmp) / "lake")
            expected = lake / "expected" / "visible_snap_001.csv"
            text = expected.read_text(encoding="utf-8")
            expected.write_text(
                text.replace("base_001,0,R0001,false", "base_001,0,R0001,true"),
                encoding="utf-8",
            )
            result = execute_audit(lake)
            rendered = render_markdown(build_report(result, "ledger.sqlite"))
            row = next(
                line for line in rendered.splitlines()
                if line.startswith("| MISSING_VISIBLE | base_001 |")
            )
            self.assertEqual(row.count("|"), 9)
            self.assertIn("DELETED_BY_EQUALITY", row)

    def test_report_files_are_byte_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = write_reports(
                Path(tmp) / "a", self.result, ["html", "md", "json"],
                "out/demo/ledger.sqlite",
            )
            second = write_reports(
                Path(tmp) / "b", self.result, ["json", "html", "md"],
                "out/demo/ledger.sqlite",
            )
            first_hashes = {item["format"]: item["sha256"] for item in first}
            second_hashes = {item["format"]: item["sha256"] for item in second}
            self.assertEqual(first_hashes, second_hashes)
            for fmt in ("json", "md", "html"):
                left = (Path(tmp) / "a" / f"report.{fmt}").read_bytes()
                right = (Path(tmp) / "b" / f"report.{fmt}").read_bytes()
                self.assertEqual(left, right)
            payload = json.loads((Path(tmp) / "a" / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["findings"], 8)


if __name__ == "__main__":
    unittest.main()
