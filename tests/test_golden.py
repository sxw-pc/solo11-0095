import json
import unittest
from collections import Counter

from support import GOLDEN, LAKE, read_json

from lakedel.engine import execute_audit
from lakedel.report import build_report, render_html, render_markdown
from lakedel.util import sha256_text


class GoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = execute_audit(LAKE)
        cls.report = build_report(cls.result, "out/demo/ledger.sqlite")

    def _summary_payload(self):
        visible = [
            f"{item.file_id}:{item.row_pos}:{item.row_id}"
            for item in self.result.decisions if item.visible
        ]
        hidden = [
            f"{item.file_id}:{item.row_pos}:{item.row_id}"
            for item in self.result.decisions if not item.visible
        ]
        json_text = json.dumps(
            self.report, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
        return {
            "stats": self.report["summary"],
            "reason_counts": self.report["visibility_summary"]["reason_counts"],
            "finding_codes": dict(
                sorted(Counter(item.code for item in self.result.findings).items())
            ),
            "diff_summary": self.report["diff_summary"],
            "visible_rows": visible,
            "hidden_rows": hidden,
            "json_sha256": sha256_text(json_text),
        }

    def test_golden_summary(self):
        golden = read_json(GOLDEN / "summary.json")
        self.assertEqual(self._summary_payload(), golden)

    def test_golden_diff_summary(self):
        self.assertEqual(
            self.report["diff_summary"], read_json(GOLDEN / "diff_summary.json")
        )

    def test_golden_markdown_snippet(self):
        snippet = (GOLDEN / "report_snippet.md").read_text(encoding="utf-8")
        markdown = render_markdown(self.report)
        self.assertIn(snippet.strip(), markdown)

    def test_golden_html_markers(self):
        markers = read_json(GOLDEN / "html_markers.json")
        html = render_html(self.report)
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
