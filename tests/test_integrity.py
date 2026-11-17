"""完整性分层证明链的隔离测试。

fixture 位于 tests/fixtures/：
- integrity_delete：可信位置删除命中 A；受损等值删除同时命中 A、B。
- integrity_data：数据文件哈希不符；可信位置删除分别命中受损文件的行与可信文件的行。
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from support import GOLDEN, LAKE, ROOT, read_json

from lakedel.engine import execute_audit
from lakedel.report import build_report, render_html, render_markdown
from lakedel.service import produce_outputs
from lakedel.util import sha256_text
from lakedel.workspace import load_workspace

FIXTURES = ROOT / "tests" / "fixtures"
DELETE_DAMAGED = FIXTURES / "integrity_delete"
DATA_DAMAGED = FIXTURES / "integrity_data"


def decision_map(result):
    return {(item.file_id, item.row_pos): item for item in result.decisions}


class DeleteIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = execute_audit(DELETE_DAMAGED)
        cls.decisions = decision_map(cls.result)

    def test_trusted_hit_proves_hidden_while_suspect_hit_does_not(self):
        a = self.decisions[("base_a", 0)]
        b = self.decisions[("base_a", 1)]
        self.assertEqual(a.certainty, "certain_hidden")
        self.assertTrue(a.certain_hidden)
        self.assertEqual(a.trusted_position_hits, ("pos_trusted",))
        self.assertEqual(a.suspect_equality_hits, ("eq_damaged",))
        # 受损等值删除虽按当前内容命中 A，也不能单独支持确定结论。
        self.assertIn("eq_damaged", a.equality_hits)
        # B 只有可疑来源命中，暂定为 hidden，但只能待核定。
        self.assertEqual(b.certainty, "indeterminate")
        self.assertTrue(b.visible is False)
        self.assertEqual(b.trusted_hits, ())
        self.assertEqual(b.suspect_equality_hits, ("eq_damaged",))
        self.assertFalse(b.certain_hidden)
        self.assertFalse(b.certain_visible)

    def test_trusted_visible_requires_excluding_untrusted_delete(self):
        c = self.decisions[("base_c", 0)]
        self.assertEqual(c.certainty, "certain_visible")
        self.assertTrue(c.certain_visible)

    def test_damaged_delete_rows_share_source_evidence(self):
        a = self.decisions[("base_a", 0)]
        b = self.decisions[("base_a", 1)]
        self.assertEqual(
            a.untrusted_equality_rows, (("base_a", 0), ("base_a", 1))
        )
        self.assertEqual(b.untrusted_equality_rows, a.untrusted_equality_rows)
        self.assertEqual(a.untrusted_equality_ids, ("eq_damaged",))
        self.assertEqual(b.untrusted_equality_ids, ("eq_damaged",))

    def test_publish_vs_investigate_authorization(self):
        outcome = self.result.outcome
        self.assertFalse(outcome.publishable)
        self.assertEqual(
            outcome.indeterminate_rows, (("base_a", 1),)
        )
        self.assertIn("UNTRUSTED_DELETE_FILE", outcome.blockers)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "investigate"
            _, _, ledger = produce_outputs(
                DELETE_DAMAGED, out, ["json"], out / "ledger.sqlite",
                view="investigate",
            )
            report = read_json(out / "report.json")
            self.assertEqual(report["conclusion"]["authorized_use"], "investigate")
            self.assertFalse(report["conclusion"]["publishable"])
            self.assertTrue(report["conclusion"]["provisional_allowed"])
            rows = {
                (item["file_id"], item["row_pos"]): item
                for item in report["row_visibility"]
            }
            self.assertTrue(rows[("base_a", 1)]["provisional_visible"] is False)
            self.assertEqual(rows[("base_a", 1)]["trusted_interval"], [False, True])
            self.assertEqual(
                rows[("base_a", 1)]["untrusted_equality_sources"], ["eq_damaged"]
            )
            self.assertFalse(rows[("base_a", 1)]["row_identity_trusted"] is False)
            connection = sqlite3.connect(str(ledger))
            try:
                stored = connection.execute(
                    """SELECT provisional_visible, certainty, indeterminate,
                              untrusted_sources_json, trusted_interval_json
                       FROM row_visibility WHERE file_id='base_a' AND row_pos=1"""
                ).fetchone()
                self.assertEqual(stored[0], 0)
                self.assertEqual(stored[1], "indeterminate")
                self.assertEqual(stored[2], 1)
                self.assertEqual(json.loads(stored[3]), ["eq_damaged"])
                self.assertEqual(json.loads(stored[4]), [False, True])
                outcome_row = connection.execute(
                    "SELECT view, publishable, publish_reason FROM outcome"
                ).fetchone()
                self.assertEqual(outcome_row[0], "investigate")
                self.assertEqual(outcome_row[1], 0)
                self.assertEqual(outcome_row[2], "INVESTIGATION_ONLY")
            finally:
                connection.close()

    def test_multiple_keys_in_damaged_file_share_all_matches(self):
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            lake = Path(tmp) / "lake"
            shutil.copytree(DELETE_DAMAGED, lake)
            # 受损文件含两个可解析键，命中的行集合必须合并而非取首个条目。
            (lake / "deletes" / "eq_damaged.jsonl").write_bytes(
                b'{"key":{"region":"north"},"reason":"a"}\n'
                b'{"key":{"region":"north"},"reason":"b"}\n'
            )
            result = execute_audit(lake)
            decisions = decision_map(result)
            expected = (("base_a", 0), ("base_a", 1))
            self.assertEqual(decisions[("base_a", 0)].untrusted_equality_rows, expected)
            self.assertEqual(decisions[("base_a", 1)].untrusted_equality_rows, expected)
            self.assertEqual(decisions[("base_a", 1)].certainty, "indeterminate")

    def test_certainty_is_conserved(self):
        summary = build_report(self.result, "ledger.sqlite")["certainty_summary"]
        self.assertTrue(summary["conserved"])
        self.assertEqual(
            summary["certain_visible"]
            + summary["certain_hidden"]
            + summary["indeterminate"],
            summary["rows"],
        )
        self.assertEqual(
            summary["provisional_visible"] + summary["provisional_hidden"],
            summary["rows"],
        )


class UnparseableDeleteTests(unittest.TestCase):
    def test_damaged_file_with_no_parseable_entries_still_blocks_visibility(self):
        """受损删除文件即使一条也解析不出，仍在其清单作用域内阻止确定可见。"""
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            lake = Path(tmp) / "lake"
            shutil.copytree(DELETE_DAMAGED, lake)
            # 字节被替换为不可解析内容；清单声明哈希仍是原内容哈希。
            (lake / "deletes" / "eq_damaged.jsonl").write_bytes(b"not-json\n")
            result = execute_audit(lake)
            decisions = decision_map(result)
            self.assertEqual(decisions[("base_a", 0)].certainty, "certain_hidden")
            self.assertEqual(decisions[("base_a", 1)].certainty, "indeterminate")
            self.assertEqual(decisions[("base_a", 1)].untrusted_sources, ("eq_damaged",))
            self.assertTrue(decisions[("base_c", 0)].certain_visible)
            self.assertIn(
                "INVALID_JSON_LINE", [item.code for item in result.findings]
            )
            self.assertFalse(result.outcome.publishable)


class DataIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = execute_audit(DATA_DAMAGED)
        cls.decisions = decision_map(cls.result)

    def test_untrusted_data_row_loses_identity_even_with_trusted_hit(self):
        x = self.decisions[("bad", 0)]
        y = self.decisions[("bad", 1)]
        self.assertEqual(x.certainty, "indeterminate")
        self.assertFalse(x.row_identity_trusted)
        self.assertEqual(x.trusted_position_hits, ("pos_trusted",))
        self.assertIn("data_source_untrusted", [step["step"] for step in x.chain])
        self.assertEqual(y.certainty, "indeterminate")
        self.assertFalse(y.row_identity_trusted)
        self.assertTrue(y.visible)

    def test_trusted_file_keeps_certainty(self):
        z = self.decisions[("good", 0)]
        self.assertEqual(z.certainty, "certain_hidden")
        self.assertTrue(z.row_identity_trusted)

    def test_publish_blocked_by_data_source(self):
        outcome = self.result.outcome
        self.assertFalse(outcome.publishable)
        self.assertIn("UNTRUSTED_DATA_FILE", outcome.blockers)
        self.assertIn("INDETERMINATE_DATA_SOURCE", outcome.blockers)
        self.assertEqual(
            outcome.indeterminate_rows, (("bad", 0), ("bad", 1))
        )


class ExpectedAndStabilityTests(unittest.TestCase):
    def test_expected_coexists_with_indeterminate_rows(self):
        """期望值本身与暂定布尔一致，但待核定行仍阻止发布。"""
        result = execute_audit(DELETE_DAMAGED)
        self.assertEqual(result.diff_summary["total"], 0)
        self.assertFalse(result.outcome.publishable)
        report = build_report(result, "ledger.sqlite")
        self.assertTrue(report["certainty_summary"]["conserved"])
        markdown = render_markdown(report)
        self.assertIn("发布核验：不可发布", markdown)
        self.assertIn("base_a", markdown)
        self.assertIn("False..True", markdown)

    def test_investigate_report_is_not_publishable(self):
        result = execute_audit(DELETE_DAMAGED)
        report = build_report(result, "ledger.sqlite", "investigate")
        self.assertFalse(report["conclusion"]["publishable"])
        self.assertIn("调查视图", render_markdown(report))
        html = render_html(report)
        self.assertIn('id="certainty"', html)
        self.assertIn('id="integrity"', html)

    def test_repeat_runs_are_byte_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = produce_outputs(
                DELETE_DAMAGED, Path(tmp) / "a", ["json", "md", "html"],
                Path(tmp) / "a" / "ledger.sqlite", view="investigate",
                ledger_label="ledger.sqlite",
            )
            second = produce_outputs(
                DELETE_DAMAGED, Path(tmp) / "b", ["html", "md", "json"],
                Path(tmp) / "b" / "ledger.sqlite", view="investigate",
                ledger_label="ledger.sqlite",
            )
            self.assertEqual(
                {item["format"]: item["sha256"] for item in first[1]},
                {item["format"]: item["sha256"] for item in second[1]},
            )
            for fmt in ("json", "md", "html"):
                self.assertEqual(
                    (Path(tmp) / "a" / f"report.{fmt}").read_bytes(),
                    (Path(tmp) / "b" / f"report.{fmt}").read_bytes(),
                )
            for db in ("ledger.sqlite",):
                left = sqlite3.connect(str(Path(tmp) / "a" / db))
                right = sqlite3.connect(str(Path(tmp) / "b" / db))
                try:
                    for table in ("row_visibility", "source_integrity", "outcome"):
                        self.assertEqual(
                            list(left.execute(f"SELECT * FROM {table} ORDER BY 1,2")),
                            list(right.execute(f"SELECT * FROM {table} ORDER BY 1,2")),
                        )
                finally:
                    left.close()
                    right.close()

    def test_old_fixture_unchanged_boolean_semantics(self):
        result = execute_audit(LAKE)
        self.assertEqual(result.stats["visible_rows"], 8)
        self.assertEqual(result.diff_summary["total"], 0)
        self.assertTrue(result.outcome.publishable)
        self.assertEqual(
            result.reason_counts,
            {
                "DELETED_BY_EQUALITY": 4,
                "DELETED_BY_POSITION": 1,
                "DELETED_BY_POSITION_AND_EQUALITY": 1,
                "VISIBLE_NO_DELETE": 7,
                "VISIBLE_STALE_DELETE_IGNORED": 1,
            },
        )
        self.assertTrue(all(item.certainty in ("certain_visible", "certain_hidden")
                            for item in result.decisions))

    def test_workspace_exposes_integrity_without_extra_findings(self):
        ws = load_workspace(DELETE_DAMAGED)
        states = {item.file_id: item for item in ws.data_integrity}
        self.assertTrue(states["base_a"].trusted)
        self.assertTrue(states["base_c"].trusted)
        self.assertEqual([item.code for item in ws.findings], [])


if __name__ == "__main__":
    unittest.main()
