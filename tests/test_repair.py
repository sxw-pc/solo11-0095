import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from support import LAKE, ROOT

from lakedel.cli import main
from lakedel.ledger import verify_ledger
from lakedel.repair import (
    build_evidence_graph,
    plan_repair,
    render_plan_markdown,
    write_plan,
)
from lakedel.schema import parse_schema
from lakedel.util import pretty_json, sha256_file

SCHEMA = {
    "columns": [
        {"name": "_row_id", "type": "string", "nullable": False},
        {"name": "_row_pos", "type": "long", "nullable": False},
        {"name": "region", "type": "string", "nullable": False},
    ]
}

ROWS = [(0, "A", "x"), (1, "B", "x"), (2, "D", "z"), (3, "E", "w")]
DELETES = [
    {"file_id": "pos_1", "kind": "position", "seq": 1, "target": "f1",
     "entries": [{"file_id": "f1", "row_pos": 0, "reason": "pos A"}]},
    {"file_id": "eq_1", "kind": "equality", "seq": 1, "columns": ["region"],
     "entries": [{"key": {"region": "x"}, "reason": "eq x"},
                 {"key": {"region": "z"}, "reason": "eq z"}]},
]
EXPECTED = [
    ("f1", 0, "A", True),
    ("f1", 1, "B", False),
    ("f1", 2, "D", False),
    ("f1", 3, "E", True),
]
BAD_DELETES = [
    {"file_id": "pos_2", "kind": "position", "seq": 0, "target": "f1",
     "entries": [{"file_id": "f1", "row_pos": 3, "reason": "stale"}]},
    {"file_id": "eq_2", "kind": "equality", "seq": 1, "columns": ["region"],
     "entries": [{"key": {"region": "q"}}, {"key": {"region": "q"}}]},
    {"file_id": "eq_3", "kind": "equality", "seq": 1, "columns": ["region"],
     "schema_hash": "0" * 64, "entries": [{"key": {"region": "x"}}]},
    {"file_id": "pos_3", "kind": "position", "seq": 1, "target": "ghost",
     "entries": [{"file_id": "ghost", "row_pos": 0}]},
]


def build_lake(root, rows=ROWS, deletes=DELETES, expected=EXPECTED):
    """隔离测试 workspace：A 被位置+等值双重删除，B/D 只被等值删除覆盖。"""
    root = Path(root)
    for directory in ("data", "deletes", "expected", "snapshots"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    schema = parse_schema(SCHEMA)
    (root / "table.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "table_id": "tbl_repair_test",
                "table_name": "修复预演隔离测试表",
                "rule_version": "MOR-1.0",
                "created_at": "2024-01-01T00:00:00Z",
                "expected_visible": "expected/visible.csv",
                "schema": SCHEMA,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    lines = ["_row_id,_row_pos,region"]
    lines += [f"{row_id},{pos},{region}" for pos, row_id, region in rows]
    (root / "data" / "f1.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    delete_specs = []
    for spec in deletes:
        path = root / "deletes" / f"{spec['file_id']}.jsonl"
        path.write_text(
            "".join(json.dumps(entry) + "\n" for entry in spec["entries"]),
            encoding="utf-8",
        )
        item = {
            "file_id": spec["file_id"],
            "path": f"deletes/{spec['file_id']}.jsonl",
            "delete_kind": spec["kind"],
            "sequence_number": spec["seq"],
            "schema_hash": spec.get("schema_hash", schema.schema_hash),
        }
        if spec.get("target"):
            item["target_file_id"] = spec["target"]
        if spec.get("columns"):
            item["equality_columns"] = spec["columns"]
        delete_specs.append(item)
    (root / "snapshots" / "snap_001.json").write_text(
        json.dumps(
            {
                "snapshot_id": "snap_001",
                "sequence_number": 5,
                "schema_hash": schema.schema_hash,
                "created_at": "2024-06-01T00:00:00Z",
                "data_files": [
                    {
                        "file_id": "f1",
                        "path": "data/f1.csv",
                        "format": "csv",
                        "row_count": len(rows),
                        "sha256": sha256_file(root / "data" / "f1.csv"),
                        "sequence_number": 1,
                        "schema_hash": schema.schema_hash,
                    }
                ],
                "delete_files": delete_specs,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    exp_lines = ["file_id,row_pos,_row_id,expected_visible"]
    exp_lines += [
        f"{file_id},{pos},{row_id},{'true' if visible else 'false'}"
        for file_id, pos, row_id, visible in expected
    ]
    (root / "expected" / "visible.csv").write_text(
        "\n".join(exp_lines) + "\n", encoding="utf-8"
    )
    return root


def invoke(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(args)
    return code, out.getvalue(), err.getvalue()


class PlanRepairTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.lake = build_lake(self.tmp / "lake")

    def test_entry_scope_suspends_overlap_and_compensates(self):
        plan = plan_repair(self.lake, scope="entry")
        summary = plan["summary"]
        self.assertTrue(plan["feasible"])
        self.assertEqual(summary["baseline_diffs"], 1)
        self.assertEqual(summary["suspended_entries"], 2)
        self.assertEqual(summary["quarantined_files"], 0)
        self.assertEqual(summary["compensation_entries"], 1)
        self.assertTrue(summary["minimal"])
        self.assertEqual(summary["residual_diffs"], 0)
        self.assertEqual(
            [action["action_id"] for action in plan["actions"]],
            ["suspend:eq_1#1", "suspend:pos_1#1", "compensate:f1#1"],
        )
        effects = {(e["file_id"], e["row_pos"]): e for e in plan["row_effects"]}
        row_a = effects[("f1", 0)]
        self.assertEqual(row_a["before_reason_code"], "DELETED_BY_POSITION_AND_EQUALITY")
        self.assertEqual(row_a["after_reason_code"], "VISIBLE_NO_DELETE")
        self.assertTrue(row_a["after_visible"])
        row_b = effects[("f1", 1)]
        self.assertEqual(row_b["before_reason_code"], "DELETED_BY_EQUALITY")
        self.assertEqual(row_b["after_reason_code"], "DELETED_BY_POSITION")
        self.assertFalse(row_b["after_visible"])
        self.assertNotIn(("f1", 2), effects)
        compensation = plan["actions"][-1]
        self.assertEqual(compensation["kind"], "compensate_position")
        self.assertEqual(compensation["evidence"]["trigger"], "REVEALED_BY_SUSPENSION")
        proposed = compensation["proposed_delete"]
        self.assertEqual(proposed["target_file_id"], "f1")
        self.assertEqual(proposed["row_pos"], 1)
        self.assertGreaterEqual(proposed["sequence_number"], 1)
        self.assertEqual(plan["rehearsal"]["residual_diffs"], [])

    def test_file_scope_quarantines_files_and_compensates_more(self):
        plan = plan_repair(self.lake, scope="file")
        summary = plan["summary"]
        self.assertTrue(plan["feasible"])
        self.assertEqual(summary["quarantined_files"], 2)
        self.assertEqual(summary["compensation_entries"], 2)
        self.assertTrue(summary["minimal"])
        self.assertEqual(summary["minimality"]["primary_metric"], "quarantined_files")
        self.assertEqual(
            [action["action_id"] for action in plan["actions"]],
            ["quarantine:eq_1", "quarantine:pos_1", "compensate:f1#1", "compensate:f1#2"],
        )
        quarantine = plan["actions"][0]
        self.assertEqual(quarantine["kind"], "quarantine_file")
        self.assertEqual(quarantine["entries"], [1, 2])
        effects = {(e["file_id"], e["row_pos"]): e for e in plan["row_effects"]}
        self.assertEqual(effects[("f1", 2)]["after_reason_code"], "DELETED_BY_POSITION")
        self.assertFalse(effects[("f1", 2)]["after_visible"])

    def test_scopes_give_different_isolation_decisions(self):
        entry_plan = plan_repair(self.lake, scope="entry")
        file_plan = plan_repair(self.lake, scope="file")
        self.assertTrue(entry_plan["feasible"])
        self.assertTrue(file_plan["feasible"])
        self.assertEqual(entry_plan["summary"]["compensation_entries"], 1)
        self.assertEqual(file_plan["summary"]["compensation_entries"], 2)
        self.assertNotEqual(
            entry_plan["summary"]["plan_key"], file_plan["summary"]["plan_key"]
        )

    def test_infeasible_when_untracked_row_revealed(self):
        lake = build_lake(self.tmp / "lake_g", rows=ROWS + [(4, "G", "x")])
        plan = plan_repair(lake, scope="entry")
        self.assertFalse(plan["feasible"])
        codes = [item["code"] for item in plan["conflicts"]]
        self.assertIn("UNTRACKED_ROW_VISIBILITY_CHANGED", codes)
        conflict = plan["conflicts"][0]
        self.assertEqual(conflict["evidence"]["file_id"], "f1")
        self.assertEqual(conflict["evidence"]["row_pos"], 4)
        self.assertEqual(
            conflict["evidence"]["revealed_by"],
            [{"delete_file_id": "eq_1", "ordinal": 1}],
        )
        self.assertFalse(plan["summary"]["minimal"] is False)
        code, output, _ = invoke(
            ["plan-repair", "--workspace", str(lake), "--out", str(self.tmp / "out_g")]
        )
        self.assertEqual(code, 2)
        self.assertIn("feasible: False", output)

    def test_infeasible_when_expected_row_not_in_data(self):
        lake = build_lake(
            self.tmp / "lake_x", expected=EXPECTED + [("f1", 9, "X", True)]
        )
        plan = plan_repair(lake, scope="entry")
        self.assertFalse(plan["feasible"])
        codes = [item["code"] for item in plan["conflicts"]]
        self.assertIn("EXPECTED_ROW_NOT_IN_DATA", codes)
        self.assertGreater(plan["summary"]["baseline_diffs"], 0)

    def test_untrustworthy_entries_are_excluded_not_suspended(self):
        lake = build_lake(self.tmp / "lake_bad", deletes=DELETES + BAD_DELETES)
        plan = plan_repair(lake, scope="entry")
        self.assertTrue(plan["feasible"])
        excluded = {
            (item["delete_file_id"], item["ordinal"]): item["conflict_code"]
            for item in plan["evidence_graph"]["excluded_entries"]
        }
        self.assertEqual(excluded[("pos_2", 1)], "STALE_SEQUENCE")
        self.assertEqual(excluded[("eq_2", 1)], "EQUALITY_NO_MATCH")
        self.assertEqual(excluded[("eq_2", 2)], "DUPLICATE_EQUALITY_DELETE")
        self.assertEqual(excluded[("eq_3", 1)], "DELETE_SCHEMA_MISMATCH")
        self.assertEqual(excluded[("pos_3", 1)], "ORPHAN_DELETE_TARGET")
        for action in plan["actions"]:
            self.assertNotIn(action.get("delete_file_id"), {"pos_2", "eq_2", "eq_3", "pos_3"})
        self.assertEqual(
            [action["action_id"] for action in plan["actions"]],
            ["suspend:eq_1#1", "suspend:pos_1#1", "compensate:f1#1"],
        )

    def test_plan_outputs_are_byte_stable(self):
        for rows, suffix in ((ROWS, "ok"), (ROWS + [(4, "G", "x")], "bad")):
            lake = build_lake(self.tmp / f"lake_{suffix}", rows=rows)
            first = plan_repair(lake, scope="entry")
            second = plan_repair(lake, scope="entry")
            self.assertEqual(pretty_json(first), pretty_json(second))
            self.assertEqual(render_plan_markdown(first), render_plan_markdown(second))
            out_a = write_plan(self.tmp / f"out_{suffix}_a", first)
            out_b = write_plan(self.tmp / f"out_{suffix}_b", second)
            for artifact_a, artifact_b in zip(out_a, out_b):
                self.assertEqual(artifact_a["sha256"], artifact_b["sha256"])
                self.assertEqual(
                    Path(artifact_a["path"]).read_bytes(),
                    Path(artifact_b["path"]).read_bytes(),
                )

    def test_examples_lake_has_empty_feasible_plan(self):
        plan = plan_repair(LAKE)
        summary = plan["summary"]
        self.assertTrue(plan["feasible"])
        self.assertEqual(summary["actions"], 0)
        self.assertEqual(summary["baseline_diffs"], 0)
        graph = plan["evidence_graph"]
        self.assertEqual(len(graph["trustworthy_entries"]), 3)
        self.assertEqual(len(graph["excluded_entries"]), 7)

    def test_evidence_graph_hits_match_visibility_semantics(self):
        plan = plan_repair(self.lake, scope="entry")
        graph = plan["evidence_graph"]
        hits = {
            (e["delete_file_id"], e["ordinal"]): [
                (h["file_id"], h["row_pos"]) for h in e["hits"]
            ]
            for e in graph["trustworthy_entries"]
        }
        self.assertEqual(hits[("pos_1", 1)], [("f1", 0)])
        self.assertEqual(hits[("eq_1", 1)], [("f1", 0), ("f1", 1)])
        self.assertEqual(hits[("eq_1", 2)], [("f1", 2)])

    def test_cli_plan_repair_subprocess(self):
        out_dir = self.tmp / "plan_out"
        proc = subprocess.run(
            [sys.executable, "-m", "lakedel", "plan-repair",
             "--workspace", str(self.lake), "--out", str(out_dir)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("feasible: True", proc.stdout)
        self.assertTrue((out_dir / "repair_plan.json").is_file())
        self.assertTrue((out_dir / "repair_plan.md").is_file())
        payload = json.loads((out_dir / "repair_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["summary"]["scope"], "entry")
        proc_file = subprocess.run(
            [sys.executable, "-m", "lakedel", "plan-repair",
             "--workspace", str(self.lake), "--out", str(self.tmp / "plan_file"),
             "--scope", "file"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(proc_file.returncode, 0, proc_file.stderr)
        self.assertIn("quarantined=2", proc_file.stdout)

    def test_cli_rejects_bad_scope_and_workspace(self):
        code, _, _ = invoke(["plan-repair", "--workspace", str(self.lake), "--scope", "row"])
        self.assertEqual(code, 3)
        code, _, _ = invoke(["plan-repair", "--workspace", "no-such-dir"])
        self.assertEqual(code, 3)

    def test_audit_embeds_and_ledger_records_plan(self):
        plan = plan_repair(self.lake, scope="entry")
        artifacts = write_plan(self.tmp / "plan", plan)
        plan_path = artifacts[0]["path"]
        out = self.tmp / "audit"
        code, _, err = invoke([
            "audit", "--workspace", str(self.lake), "--out", str(out),
            "--db", str(out / "ledger.sqlite"), "--format", "json,md,html",
            "--repair-plan", str(plan_path),
        ])
        # 该 fixture 基线存在 1 处期望差异，audit 返回 2；计划引用仍应嵌入。
        self.assertEqual(code, 2, err)
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(
            report["repair_plan"]["plan_key"], plan["summary"]["plan_key"]
        )
        self.assertEqual(report["repair_plan"]["scope"], "entry")
        self.assertTrue(report["repair_plan"]["feasible"])
        markdown = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("## 修复预演计划", markdown)
        html = (out / "report.html").read_text(encoding="utf-8")
        self.assertIn('id="repair-plan"', html)
        connection = sqlite3.connect(str(out / "ledger.sqlite"))
        try:
            rows = connection.execute(
                "SELECT plan_key, scope, feasible, actions, compensations "
                "FROM repair_plans"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], plan["summary"]["plan_key"])
        self.assertEqual(rows[0][3:], (3, 1))
        self.assertTrue(verify_ledger(out / "ledger.sqlite")["ok"])

    def test_audit_rejects_mismatched_plan(self):
        other = build_lake(self.tmp / "lake_other", rows=ROWS + [(4, "G", "x")])
        plan = plan_repair(other, scope="entry")
        artifacts = write_plan(self.tmp / "plan_other", plan)
        code, _, err = invoke([
            "audit", "--workspace", str(self.lake), "--out", str(self.tmp / "audit_x"),
            "--format", "json", "--repair-plan", str(artifacts[0]["path"]),
        ])
        self.assertEqual(code, 3)
        self.assertIn("REPAIR_PLAN_MISMATCH", err)
        code, _, err = invoke([
            "audit", "--workspace", str(self.lake), "--out", str(self.tmp / "audit_y"),
            "--format", "json", "--repair-plan", str(self.tmp / "missing.json"),
        ])
        self.assertEqual(code, 3)
        self.assertIn("REPAIR_PLAN_INVALID", err)


if __name__ == "__main__":
    unittest.main()
