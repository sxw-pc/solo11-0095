"""合同化精确整数统计披露的隔离测试。

覆盖：删前 33 与删后 32 差别、13−6−5 / 9−7 重构、两个合同各自的最小隐藏
与三份见证、边际线性依赖（暴力枚举交叉验证）、多敏感格、输入重排/同值、
无可发布方案、旧 run_key、无效行列或人数。
"""

import io
import itertools
import json
import contextlib
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lakedel.attendance_disclosure import (
    CELL_BOUND,
    MIN_FEASIBLE_VALUES,
    build_release,
    plan_disclosure,
    validate_grid_spec,
)
from lakedel.attendance_release_intake import main as intake_main
from lakedel.attendance_release_intake import naive_preview
from lakedel.engine import execute_audit
from lakedel.models import RowDecision, RowRecord


ROOT = Path(__file__).resolve().parents[1]
LAKE = ROOT / "examples" / "attendance_release" / "lake"
INTAKE_PATH = ROOT / "examples" / "attendance_release" / "intake.json"
WITNESS_PATH = ROOT / "examples" / "attendance_release" / "witness.json"

CONTRACTS = {
    "margin_priority": "retain_all_row_and_column_totals",
    "detail_priority": "retain_all_nonsensitive_cells",
}
ROWS = ["local", "visiting"]
COLUMNS = ["morning", "afternoon", "evening"]
COUNTS = {
    "local": {"morning": 2, "afternoon": 6, "evening": 5},
    "visiting": {"morning": 7, "afternoon": 4, "evening": 8},
}


def load_intake():
    return json.loads(INTAKE_PATH.read_text(encoding="utf-8"))


def load_witness():
    return json.loads(WITNESS_PATH.read_text(encoding="utf-8"))


def brute_feasible_values(counts, rows, columns, hidden_cells, hidden_rows,
                          hidden_columns, cell, bound=CELL_BOUND):
    """独立暴力枚举：隐藏格取遍 0..bound，核对已发布边际，收集目标格可取值。"""
    row_totals = {r: sum(counts[r][c] for c in columns) for r in rows}
    col_totals = {c: sum(counts[r][c] for r in rows) for c in columns}
    hidden = sorted(hidden_cells)
    values = set()
    for assignment in itertools.product(range(bound + 1), repeat=len(hidden)):
        grid = {(r, c): counts[r][c] for r in rows for c in columns}
        grid.update(zip(hidden, assignment))
        if any(r not in hidden_rows
               and sum(grid[(r, c)] for c in columns) != row_totals[r] for r in rows):
            continue
        if any(c not in hidden_columns
               and sum(grid[(r, c)] for r in rows) != col_totals[c] for c in columns):
            continue
        values.add(grid[cell])
    return sorted(values)


def plan_hidden_sets(plan):
    cells = {tuple(cell_id.split(":")) for cell_id in plan["hidden_cells"]}
    return cells, set(plan["hidden_row_totals"]), set(plan["hidden_column_totals"])


def assert_witnesses_compatible(testcase, plan, counts, rows, columns, bound=CELL_BOUND):
    """每份见证必须等于已发布精确值、满足已发布边际、落在 0..bound 内。"""
    row_totals = {r: sum(counts[r][c] for c in columns) for r in rows}
    col_totals = {c: sum(counts[r][c] for r in rows) for c in columns}
    hidden_cells, hidden_rows, hidden_columns = plan_hidden_sets(plan)
    testcase.assertGreaterEqual(len(plan["witnesses"]), MIN_FEASIBLE_VALUES)
    for witness in plan["witnesses"]:
        grid = witness["grid"]
        for row in rows:
            for col in columns:
                value = grid[row][col]
                testcase.assertGreaterEqual(value, 0)
                testcase.assertLessEqual(value, bound)
                if (row, col) not in hidden_cells:
                    testcase.assertEqual(value, counts[row][col])
        for row in rows:
            if row not in hidden_rows:
                testcase.assertEqual(sum(grid[row][c] for c in columns), row_totals[row])
        for col in columns:
            if col not in hidden_columns:
                testcase.assertEqual(sum(grid[r][col] for r in rows), col_totals[col])
        for cell_id, value in witness["sensitive_values"].items():
            row, col = cell_id.split(":")
            testcase.assertEqual(grid[row][col], value)


class AuditBindingTests(unittest.TestCase):
    """删前 33 与删后 32 的差别必须经 execute_audit 传导到披露。"""

    def test_post_delete_counts_and_binding(self):
        result = execute_audit(LAKE)
        self.assertEqual(result.stats["rows"], 33)
        self.assertEqual(result.stats["visible_rows"], 32)
        self.assertEqual(result.stats["hidden_rows"], 1)
        self.assertEqual(result.stats["diffs"], 0)
        report = build_release(load_intake(), result)
        self.assertEqual(report["run_key"], load_intake()["run_key"])
        self.assertEqual(report["visible_rows"], 32)
        self.assertEqual(report["hidden_rows"], 1)
        self.assertEqual(report["counts"], load_witness()["audited_visible_grid"])
        self.assertEqual(report["row_totals"], {"local": 13, "visiting": 19})
        self.assertEqual(report["column_totals"], {"morning": 9, "afternoon": 10, "evening": 13})
        self.assertEqual(report["sensitive_cells"], ["local:morning"])

    def test_pre_delete_snapshot_has_33_visible_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake = Path(tmp) / "lake"
            shutil.copytree(LAKE, lake)
            snapshot_path = lake / "snapshots" / "snap_001.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["delete_files"] = []
            snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
            expected = lake / "expected" / "visible.csv"
            expected.write_text(
                expected.read_text(encoding="utf-8").replace("base,2,R0003,false", "base,2,R0003,true"),
                encoding="utf-8",
            )
            result = execute_audit(lake)
            self.assertEqual(result.stats["visible_rows"], 33)
            self.assertEqual(result.stats["diffs"], 0)
            data = load_intake()
            with self.assertRaises(ValueError):
                build_release(data, result)  # 旧 run_key 不得沿用
            data["run_key"] = result.run_key
            report = build_release(data, result)
            self.assertEqual(report["counts"]["local"]["morning"], 3)
            self.assertEqual(report["sensitive_cells"], [])
            self.assertEqual(report["contracts"]["margin_priority"]["hidden_count"], 0)

    def test_delete_evidence_change_propagates_to_disclosure(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake = Path(tmp) / "lake"
            shutil.copytree(LAKE, lake)
            deletes = lake / "deletes" / "pos.jsonl"
            with deletes.open("a", encoding="utf-8") as handle:
                handle.write('{"file_id":"base","row_pos":3,"reason":"synthetic opt-out"}\n')
            expected = lake / "expected" / "visible.csv"
            expected.write_text(
                expected.read_text(encoding="utf-8").replace("base,3,R0004,true", "base,3,R0004,false"),
                encoding="utf-8",
            )
            result = execute_audit(lake)
            self.assertEqual(result.stats["visible_rows"], 31)
            self.assertEqual(result.stats["diffs"], 0)
            self.assertNotEqual(result.run_key, load_intake()["run_key"])
            with self.assertRaises(ValueError):
                build_release(load_intake(), result)  # 旧报表不得沿用
            data = load_intake()
            data["run_key"] = result.run_key
            report = build_release(data, result)
            self.assertEqual(report["counts"]["local"]["afternoon"], 5)
            self.assertEqual(report["row_totals"], {"local": 12, "visiting": 19})
            self.assertEqual(report["column_totals"]["afternoon"], 9)
            self.assertNotEqual(report["counts"], load_witness()["audited_visible_grid"])

    def test_row_value_change_propagates_to_disclosure(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake = Path(tmp) / "lake"
            shutil.copytree(LAKE, lake)
            base = lake / "data" / "base.csv"
            lines = base.read_text(encoding="utf-8").splitlines()
            # R0001: local,morning -> visiting,morning（可见行的取值变化）
            lines[1] = lines[1].replace("local,morning", "visiting,morning")
            base.write_text("\n".join(lines) + "\n", encoding="utf-8")
            result = execute_audit(lake)
            self.assertEqual(result.stats["visible_rows"], 32)
            self.assertEqual(result.stats["diffs"], 0)
            self.assertNotEqual(result.run_key, load_intake()["run_key"])
            with self.assertRaises(ValueError):
                build_release(load_intake(), result)  # 旧报表不得沿用
            data = load_intake()
            data["run_key"] = result.run_key
            report = build_release(data, result)
            self.assertEqual(report["counts"]["local"]["morning"], 1)
            self.assertEqual(report["counts"]["visiting"]["morning"], 8)
            self.assertEqual(report["column_totals"]["morning"], 9)

    def test_expected_change_breaks_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake = Path(tmp) / "lake"
            shutil.copytree(LAKE, lake)
            expected = lake / "expected" / "visible.csv"
            expected.write_text(
                expected.read_text(encoding="utf-8").replace("base,3,R0004,true", "base,3,R0004,false"),
                encoding="utf-8",
            )
            result = execute_audit(lake)
            self.assertGreater(result.stats["diffs"], 0)
            with self.assertRaises(ValueError):
                build_release(load_intake(), result)

    def test_stale_run_key_rejected(self):
        result = execute_audit(LAKE)
        data = load_intake()
        data["run_key"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "different audit"):
            build_release(data, result)
        data = load_intake()
        data["snapshot_id"] = "snap_999"
        with self.assertRaisesRegex(ValueError, "different audit"):
            build_release(data, result)

    def test_unreconciled_diffs_rejected(self):
        result = execute_audit(LAKE)
        data = load_intake()
        stub = SimpleNamespace(
            workspace=result.workspace,
            decisions=result.decisions,
            run_key=result.run_key,
            stats={**result.stats, "diffs": 1},
        )
        with self.assertRaisesRegex(ValueError, "unreconciled"):
            build_release(data, stub)


class NaiveLeakTests(unittest.TestCase):
    """naive 初稿的 13−6−5 与 9−7 两条重构路径。"""

    def test_naive_preview_leaks_masked_cell_two_ways(self):
        result = execute_audit(LAKE)
        report = naive_preview(load_intake(), result)
        self.assertEqual(report["masked_cells"], ["local:morning"])
        row_leak = (report["row_totals"]["local"] - report["visible_cells"]["local:afternoon"]
                    - report["visible_cells"]["local:evening"])
        col_leak = report["column_totals"]["morning"] - report["visible_cells"]["visiting:morning"]
        self.assertEqual(row_leak, 2)
        self.assertEqual(col_leak, 2)

    def test_certificate_records_exact_reconstruction(self):
        plan = plan_disclosure(COUNTS, ROWS, COLUMNS, 3, CONTRACTS)
        for name in CONTRACTS:
            rejected = plan["contracts"][name]["certificate"]["rejected_candidates"]
            solo = next(item for item in rejected if item["hidden"] == ["local:morning"])
            self.assertEqual(solo["failures"],
                             [{"cell": "local:morning", "feasible_values": [2]}])


class ContractPlanTests(unittest.TestCase):
    """两个合同各自的最小隐藏、可取值与三份见证。"""

    @classmethod
    def setUpClass(cls):
        cls.witness = load_witness()
        cls.plan = plan_disclosure(COUNTS, ROWS, COLUMNS, 3, CONTRACTS)

    def test_margin_priority_hides_four_cells_keeps_all_margins(self):
        plan = self.plan["contracts"]["margin_priority"]
        self.assertEqual(plan["status"], "issued")
        self.assertEqual(plan["hidden_count"], 4)
        self.assertEqual(plan["hidden_cells"], sorted(self.witness["margin_priority_hidden_cells"]))
        self.assertEqual(plan["hidden_row_totals"], [])
        self.assertEqual(plan["hidden_column_totals"], [])
        self.assertEqual(plan["published_row_totals"], {"local": 13, "visiting": 19})
        self.assertEqual(plan["published_column_totals"],
                         {"morning": 9, "afternoon": 10, "evening": 13})
        self.assertEqual(plan["published_cells"], {"local:evening": 5, "visiting:evening": 8})
        self.assertEqual(plan["sensitive_cell_values"], {"local:morning": list(range(9))})

    def test_margin_priority_witnesses_match_recorded_alternatives(self):
        plan = self.plan["contracts"]["margin_priority"]
        assert_witnesses_compatible(self, plan, COUNTS, ROWS, COLUMNS)
        self.assertEqual({w["sensitive_values"]["local:morning"] for w in plan["witnesses"]},
                         {1, 2, 3})
        grids = {json.dumps(w["grid"], sort_keys=True) for w in plan["witnesses"]}
        expected = {json.dumps(g, sort_keys=True)
                    for g in [self.witness["audited_visible_grid"], *self.witness["alternative_grids"]]}
        self.assertEqual(grids, expected)

    def test_margin_priority_certificate_proves_minimality(self):
        plan = self.plan["contracts"]["margin_priority"]
        certificate = plan["certificate"]
        self.assertTrue(certificate["exhaustive"])
        self.assertEqual(certificate["chosen_hidden_count"], 4)
        rejected = certificate["rejected_candidates"]
        smaller = [item for item in rejected if len(item["hidden"]) < 4]
        self.assertEqual(len(smaller), certificate["smaller_candidate_count"])
        self.assertEqual(len(smaller), 1 + 5 + 10)  # 敏感格之外 0/1/2 个额外隐藏
        for item in rejected:
            self.assertTrue(item["failures"])
            for failure in item["failures"]:
                self.assertLess(len(failure["feasible_values"]), MIN_FEASIBLE_VALUES)
        # 隐藏敏感格但保留全部行列合计：被行/列合计唯一确定为 2
        solo = next(item for item in rejected if item["hidden"] == ["local:morning"])
        self.assertEqual(solo["failures"][0]["feasible_values"], [2])

    def test_detail_priority_hides_two_marginals_keeps_five_cells(self):
        plan = self.plan["contracts"]["detail_priority"]
        self.assertEqual(plan["status"], "issued")
        self.assertEqual(plan["hidden_count"], 3)
        self.assertEqual(plan["hidden_cells"], ["local:morning"])
        self.assertEqual(plan["hidden_row_totals"], self.witness["detail_priority_hidden_row_totals"])
        self.assertEqual(plan["hidden_column_totals"],
                         self.witness["detail_priority_hidden_column_totals"])
        self.assertEqual(plan["published_cells"], {
            "local:afternoon": 6, "local:evening": 5,
            "visiting:morning": 7, "visiting:afternoon": 4, "visiting:evening": 8,
        })
        self.assertEqual(plan["published_row_totals"], {"visiting": 19})
        self.assertEqual(plan["published_column_totals"], {"afternoon": 10, "evening": 13})
        self.assertEqual(plan["sensitive_cell_values"],
                         {"local:morning": list(range(CELL_BOUND + 1))})

    def test_detail_priority_witnesses_and_certificate(self):
        plan = self.plan["contracts"]["detail_priority"]
        assert_witnesses_compatible(self, plan, COUNTS, ROWS, COLUMNS)
        self.assertEqual({w["sensitive_values"]["local:morning"] for w in plan["witnesses"]},
                         {1, 2, 3})
        certificate = plan["certificate"]
        self.assertEqual(certificate["chosen_hidden_count"], 3)
        rejected = certificate["rejected_candidates"]
        smaller = [item for item in rejected if len(item["hidden"]) < 3]
        self.assertEqual(len(smaller), 1 + 5)  # 敏感格之外 0/1 个边际
        # 只隐藏其中一个边际仍漏出 2
        row_only = next(item for item in rejected
                        if item["hidden"] == ["local:morning", "row:local"])
        self.assertEqual(row_only["failures"][0]["feasible_values"], [2])
        col_only = next(item for item in rejected
                        if item["hidden"] == ["column:morning", "local:morning"])
        self.assertEqual(col_only["failures"][0]["feasible_values"], [2])

    def test_feasible_values_match_brute_force(self):
        for name in CONTRACTS:
            plan = self.plan["contracts"][name]
            hidden_cells, hidden_rows, hidden_columns = plan_hidden_sets(plan)
            brute = brute_feasible_values(COUNTS, ROWS, COLUMNS, hidden_cells,
                                          hidden_rows, hidden_columns, ("local", "morning"))
            self.assertEqual(plan["sensitive_cell_values"]["local:morning"], brute)
            self.assertGreaterEqual(len(brute), MIN_FEASIBLE_VALUES)


class MarginalDependencyTests(unittest.TestCase):
    """边际线性依赖：行合计之和 == 列合计之和，流量模型与暴力枚举一致。"""

    def test_all_cells_hidden_all_margins_published(self):
        counts = {"a": {"x": 2, "y": 1}, "b": {"x": 1, "y": 2}}
        rows, columns = ["a", "b"], ["x", "y"]
        plan = plan_disclosure(counts, rows, columns, 3, CONTRACTS)
        margin = plan["contracts"]["margin_priority"]
        self.assertEqual(margin["status"], "issued")
        self.assertEqual(margin["hidden_count"], 4)  # 全部格子敏感，全部隐藏
        hidden_cells, hidden_rows, hidden_columns = plan_hidden_sets(margin)
        for cell in [("a", "x"), ("a", "y"), ("b", "x"), ("b", "y")]:
            brute = brute_feasible_values(counts, rows, columns, hidden_cells,
                                          hidden_rows, hidden_columns, cell)
            self.assertEqual(margin["sensitive_cell_values"][f"{cell[0]}:{cell[1]}"], brute)
            self.assertGreaterEqual(len(brute), MIN_FEASIBLE_VALUES)

    def test_hidden_marginal_dependency_matches_brute_force(self):
        counts = {"a": {"x": 2, "y": 1}, "b": {"x": 1, "y": 2}}
        rows, columns = ["a", "b"], ["x", "y"]
        plan = plan_disclosure(counts, rows, columns, 3, CONTRACTS)
        detail = plan["contracts"]["detail_priority"]
        self.assertEqual(detail["status"], "issued")
        hidden_cells, hidden_rows, hidden_columns = plan_hidden_sets(detail)
        for cell in [("a", "x"), ("a", "y"), ("b", "x")]:
            brute = brute_feasible_values(counts, rows, columns, hidden_cells,
                                          hidden_rows, hidden_columns, cell)
            self.assertEqual(detail["sensitive_cell_values"][f"{cell[0]}:{cell[1]}"], brute)

    def test_redundant_marginal_still_binds_cells(self):
        # 2×2 全隐藏、全边际发布时只有一个自由度；可取值是区间而非笛卡尔积
        counts = {"a": {"x": 2, "y": 2}, "b": {"x": 2, "y": 2}}
        plan = plan_disclosure(counts, ["a", "b"], ["x", "y"], 3, CONTRACTS)
        margin = plan["contracts"]["margin_priority"]
        self.assertEqual(margin["sensitive_cell_values"]["a:x"], [0, 1, 2, 3, 4])
        hidden_cells, hidden_rows, hidden_columns = plan_hidden_sets(margin)
        brute = brute_feasible_values(counts, ["a", "b"], ["x", "y"], hidden_cells,
                                      hidden_rows, hidden_columns, ("a", "x"))
        self.assertEqual(brute, [0, 1, 2, 3, 4])


class MultipleSensitiveCellsTests(unittest.TestCase):
    COUNTS2 = {
        "local": {"morning": 2, "afternoon": 1, "evening": 5},
        "visiting": {"morning": 7, "afternoon": 4, "evening": 8},
    }

    @classmethod
    def setUpClass(cls):
        cls.plan = plan_disclosure(cls.COUNTS2, ROWS, COLUMNS, 3, CONTRACTS)

    def test_both_sensitive_cells_listed(self):
        self.assertEqual(self.plan["sensitive_cells"], ["local:afternoon", "local:morning"])

    def test_margin_priority_covers_both_with_one_cycle(self):
        plan = self.plan["contracts"]["margin_priority"]
        self.assertEqual(plan["status"], "issued")
        self.assertEqual(plan["hidden_cells"], ["local:afternoon", "local:morning",
                                                "visiting:afternoon", "visiting:morning"])
        self.assertEqual(plan["sensitive_cell_values"]["local:morning"], [0, 1, 2, 3])
        self.assertEqual(plan["sensitive_cell_values"]["local:afternoon"], [0, 1, 2, 3])
        hidden_cells, hidden_rows, hidden_columns = plan_hidden_sets(plan)
        for cell in [("local", "morning"), ("local", "afternoon")]:
            brute = brute_feasible_values(self.COUNTS2, ROWS, COLUMNS, hidden_cells,
                                          hidden_rows, hidden_columns, cell)
            self.assertEqual(plan["sensitive_cell_values"][f"{cell[0]}:{cell[1]}"], brute)
        assert_witnesses_compatible(self, plan, self.COUNTS2, ROWS, COLUMNS)
        for cell_id in ["local:morning", "local:afternoon"]:
            seen = {w["sensitive_values"][cell_id] for w in plan["witnesses"]}
            self.assertGreaterEqual(len(seen), MIN_FEASIBLE_VALUES)

    def test_detail_priority_hides_two_column_totals(self):
        plan = self.plan["contracts"]["detail_priority"]
        self.assertEqual(plan["status"], "issued")
        self.assertEqual(plan["hidden_cells"], ["local:afternoon", "local:morning"])
        self.assertEqual(plan["hidden_row_totals"], [])
        self.assertEqual(plan["hidden_column_totals"], ["afternoon", "morning"])
        # local 行合计 8 仍发布：两格之和为 3，各自可取 0..3
        self.assertEqual(plan["published_row_totals"], {"local": 8, "visiting": 19})
        self.assertEqual(plan["sensitive_cell_values"]["local:morning"], [0, 1, 2, 3])
        self.assertEqual(plan["sensitive_cell_values"]["local:afternoon"], [0, 1, 2, 3])
        hidden_cells, hidden_rows, hidden_columns = plan_hidden_sets(plan)
        for cell in [("local", "morning"), ("local", "afternoon")]:
            brute = brute_feasible_values(self.COUNTS2, ROWS, COLUMNS, hidden_cells,
                                          hidden_rows, hidden_columns, cell)
            self.assertEqual(plan["sensitive_cell_values"][f"{cell[0]}:{cell[1]}"], brute)


class MaxGridTests(unittest.TestCase):
    """3×4 上限网格：小上限下与暴力枚举逐格交叉验证。"""

    ROWS3 = ["r1", "r2", "r3"]
    COLS4 = ["c1", "c2", "c3", "c4"]
    COUNTS34 = {
        "r1": {"c1": 2, "c2": 4, "c3": 3, "c4": 1},
        "r2": {"c1": 4, "c2": 3, "c3": 4, "c4": 3},
        "r3": {"c1": 3, "c2": 4, "c3": 2, "c4": 4},
    }
    BOUND = 4

    @classmethod
    def setUpClass(cls):
        cls.plan = plan_disclosure(cls.COUNTS34, cls.ROWS3, cls.COLS4, 3, CONTRACTS,
                                   cell_bound=cls.BOUND)

    def test_three_by_four_grid_issued(self):
        self.assertEqual(self.plan["sensitive_cells"], ["r1:c1", "r1:c4", "r3:c3"])
        for name in CONTRACTS:
            plan = self.plan["contracts"][name]
            self.assertEqual(plan["status"], "issued")
            assert_witnesses_compatible(self, plan, self.COUNTS34, self.ROWS3,
                                        self.COLS4, bound=self.BOUND)
            for cell_id in self.plan["sensitive_cells"]:
                seen = {w["sensitive_values"][cell_id] for w in plan["witnesses"]}
                self.assertGreaterEqual(len(seen), MIN_FEASIBLE_VALUES)

    def test_three_by_four_feasible_values_match_brute_force(self):
        for name in CONTRACTS:
            plan = self.plan["contracts"][name]
            hidden_cells, hidden_rows, hidden_columns = plan_hidden_sets(plan)
            for cell_id in self.plan["sensitive_cells"]:
                cell = tuple(cell_id.split(":"))
                brute = brute_feasible_values(self.COUNTS34, self.ROWS3, self.COLS4,
                                              hidden_cells, hidden_rows, hidden_columns,
                                              cell, bound=self.BOUND)
                self.assertEqual(plan["sensitive_cell_values"][cell_id], brute,
                                 msg=f"{name} {cell_id}")
                self.assertGreaterEqual(len(brute), MIN_FEASIBLE_VALUES)

    def test_oversized_grid_rejected(self):
        with self.assertRaises(ValueError):
            plan_disclosure(self.COUNTS34, ["r1", "r2", "r3", "r4"], self.COLS4, 3, CONTRACTS)
        with self.assertRaises(ValueError):
            plan_disclosure(self.COUNTS34, self.ROWS3, ["c1", "c2", "c3", "c4", "c5"],
                            3, CONTRACTS)


class ReorderingAndTiesTests(unittest.TestCase):
    def test_row_column_order_does_not_change_plans(self):
        base = plan_disclosure(COUNTS, ROWS, COLUMNS, 3, CONTRACTS)
        reordered_counts = {
            "visiting": {"evening": 8, "afternoon": 4, "morning": 7},
            "local": {"evening": 5, "afternoon": 6, "morning": 2},
        }
        reordered = plan_disclosure(reordered_counts, ["visiting", "local"],
                                    ["evening", "afternoon", "morning"], 3, CONTRACTS)
        self.assertEqual(base["contracts"], reordered["contracts"])
        self.assertEqual(base["sensitive_cells"], reordered["sensitive_cells"])

    def test_equal_values_stay_deterministic(self):
        counts = {"a": {"x": 2, "y": 2}, "b": {"x": 2, "y": 2}}
        first = plan_disclosure(counts, ["a", "b"], ["x", "y"], 3, CONTRACTS)
        second = plan_disclosure(counts, ["a", "b"], ["x", "y"], 3, CONTRACTS)
        self.assertEqual(first, second)
        margin = first["contracts"]["margin_priority"]
        self.assertEqual(margin["status"], "issued")
        self.assertEqual(margin["hidden_count"], 4)
        detail = first["contracts"]["detail_priority"]
        self.assertEqual(detail["status"], "issued")
        self.assertEqual(detail["hidden_count"], 4)  # 四格全敏感，边际零隐藏
        self.assertEqual(detail["hidden_row_totals"], [])
        self.assertEqual(detail["hidden_column_totals"], [])

    def test_tie_break_prefers_sorted_identifiers(self):
        # 隐藏 local 侧下午块与晚上块同为 4 格解，排序标识下午块在前
        plan = plan_disclosure(COUNTS, ROWS, COLUMNS, 3, CONTRACTS)
        self.assertEqual(plan["contracts"]["margin_priority"]["hidden_cells"],
                         ["local:afternoon", "local:morning",
                          "visiting:afternoon", "visiting:morning"])


class RefusalTests(unittest.TestCase):
    def test_single_row_grid_cannot_hide_from_column_totals(self):
        counts = {"only": {"a": 2, "b": 5, "c": 6}}
        plan = plan_disclosure(counts, ["only"], ["a", "b", "c"], 3, CONTRACTS)
        margin = plan["contracts"]["margin_priority"]
        self.assertEqual(margin["status"], "refused")
        self.assertIn("no publishable scheme", margin["reason"])
        maximal = margin["certificate"]["maximal_candidate"]
        self.assertEqual(maximal["failures"][0]["feasible_values"], [2])
        detail = plan["contracts"]["detail_priority"]
        self.assertEqual(detail["status"], "issued")  # 行合计与列合计都可隐藏

    def test_tight_two_by_two_margins_refuse_margin_contract(self):
        counts = {"a": {"x": 1, "y": 0}, "b": {"x": 0, "y": 1}}
        plan = plan_disclosure(counts, ["a", "b"], ["x", "y"], 3, CONTRACTS)
        margin = plan["contracts"]["margin_priority"]
        self.assertEqual(margin["status"], "refused")
        maximal = margin["certificate"]["maximal_candidate"]
        self.assertEqual(len(maximal["hidden"]), 4)
        self.assertEqual(maximal["failures"][0]["feasible_values"], [0, 1])
        detail = plan["contracts"]["detail_priority"]
        self.assertEqual(detail["status"], "issued")

    def test_no_sensitive_cells_issues_empty_hiding(self):
        plan = plan_disclosure(COUNTS, ROWS, COLUMNS, 1, CONTRACTS)
        self.assertEqual(plan["sensitive_cells"], [])
        for name in CONTRACTS:
            contract = plan["contracts"][name]
            self.assertEqual(contract["status"], "issued")
            self.assertEqual(contract["hidden_count"], 0)
            self.assertEqual(contract["witnesses"], [])


class ValidationTests(unittest.TestCase):
    def test_invalid_grid_specs(self):
        bad_specs = [
            ([], ["a"], 3),                       # 空行
            (["a", "b", "c", "d"], ["a"], 3),     # 超过 3 行
            (["a"], ["a", "b", "c", "d", "e"], 3),  # 超过 4 列
            (["a", "a"], ["b"], 3),               # 重复行
            (["a"], ["b", "b"], 3),               # 重复列
            (["a:1"], ["b"], 3),                  # 非法字符
            ([""], ["b"], 3),                     # 空标签
            (["a"], ["b"], 0),                    # 阈值过小
            (["a"], ["b"], "3"),                  # 阈值类型
            (["a"], ["b"], True),                 # 阈值布尔
        ]
        for rows, columns, mask_below in bad_specs:
            with self.assertRaises(ValueError, msg=f"{rows} {columns} {mask_below}"):
                validate_grid_spec(rows, columns, mask_below)

    def test_invalid_counts(self):
        rows, columns = ["a"], ["x"]
        base = {"a": {"x": 2}}
        for counts in [
            {"a": {"x": -1}, "dummy": {}},        # 行不匹配
            {"a": {"x": -1}},                     # 负数
            {"a": {"x": CELL_BOUND + 1}},         # 超过每格上限
            {"a": {"x": "2"}},                    # 非整数
            {"a": {"x": True}},                   # 布尔
            {"a": {}},                            # 缺列
        ]:
            with self.assertRaises(ValueError, msg=str(counts)):
                plan_disclosure(counts, rows, columns, 3, CONTRACTS)
        self.assertEqual(plan_disclosure(base, rows, columns, 3, CONTRACTS)["contracts"]
                         ["margin_priority"]["status"], "refused")

    def test_invalid_contracts(self):
        with self.assertRaises(ValueError):
            plan_disclosure({"a": {"x": 2}}, ["a"], ["x"], 3, {})
        with self.assertRaises(ValueError):
            plan_disclosure({"a": {"x": 2}}, ["a"], ["x"], 3,
                            {"mystery": "retain_nothing"})

    def test_visible_record_outside_declared_grid(self):
        data = load_intake()
        record = RowRecord("base", 0, "R0001", {"category": "remote", "region": "morning"})
        decision = RowDecision("base", 0, "R0001", True, "VISIBLE_NO_DELETE")
        stub = SimpleNamespace(
            workspace=SimpleNamespace(
                snapshot=SimpleNamespace(snapshot_id=data["snapshot_id"]),
                rows=[record],
            ),
            decisions=[decision],
            run_key=data["run_key"],
            stats={"diffs": 0, "visible_rows": 1, "hidden_rows": 0},
        )
        with self.assertRaisesRegex(ValueError, "outside declared grid"):
            build_release(data, stub)


class CliTests(unittest.TestCase):
    def test_main_issues_signed_disclosure(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = intake_main(["--fixture", str(INTAKE_PATH)])
        self.assertEqual(code, 0)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["run_key"], load_intake()["run_key"])
        self.assertEqual(report["visible_rows"], 32)
        self.assertEqual(report["contracts"]["margin_priority"]["status"], "issued")
        self.assertEqual(report["contracts"]["detail_priority"]["status"], "issued")
        self.assertEqual(report["contracts"]["margin_priority"]["hidden_count"], 4)
        self.assertEqual(report["contracts"]["detail_priority"]["hidden_count"], 3)


if __name__ == "__main__":
    unittest.main()
