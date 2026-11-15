import unittest

from lakedel.models import DataFileSpec, DeleteOperation, RowRecord
from lakedel.visibility import compute_visibility, visibility_by_file


def spec(file_id, sequence):
    return DataFileSpec(file_id, f"data/{file_id}.csv", "csv", 3, "0" * 64,
                        sequence, "f" * 64)


def row(file_id, pos, row_id, region):
    return RowRecord(file_id, pos, row_id, {"_row_pos": pos, "region": region})


class VisibilityTests(unittest.TestCase):
    def setUp(self):
        self.data = (spec("f_old", 2), spec("f_new", 4))
        self.rows = (
            row("f_old", 0, "A", "north"),
            row("f_old", 1, "B", "south"),
            row("f_new", 0, "C", "north"),
            row("f_new", 1, "D", "east"),
            row("f_new", 2, "E", "south"),
        )
        self.ops = (
            DeleteOperation("pos_a", 1, "position", 3, "f_old", 0, None, "x"),
            DeleteOperation(
                "eq_a", 1, "equality", 4, None, None, {"region": "north"}, "x",
                ("region",), True, "",
            ),
            DeleteOperation(
                "pos_stale", 1, "position", 3, "f_new", 1, None, "stale",
                (), False, "STALE_SEQUENCE",
            ),
            DeleteOperation(
                "eq_old", 1, "equality", 1, None, None, {"region": "north"}, "old",
                ("region",), True, "",
            ),
        )

    def test_combined_rules(self):
        decisions, counts = compute_visibility(self.rows, self.data, self.ops)
        by_id = {item.row_id: item for item in decisions}
        self.assertFalse(by_id["A"].visible)
        self.assertEqual(by_id["A"].reason_code, "DELETED_BY_POSITION_AND_EQUALITY")
        self.assertFalse(by_id["C"].visible)
        self.assertEqual(by_id["C"].reason_code, "DELETED_BY_EQUALITY")
        self.assertTrue(by_id["D"].visible)
        self.assertEqual(by_id["D"].reason_code, "VISIBLE_STALE_DELETE_IGNORED")
        self.assertEqual(by_id["D"].stale_hits, ("pos_stale",))
        self.assertTrue(by_id["B"].visible)
        self.assertEqual(by_id["B"].reason_code, "VISIBLE_NO_DELETE")
        self.assertEqual(
            counts,
            {
                "DELETED_BY_EQUALITY": 1,
                "DELETED_BY_POSITION_AND_EQUALITY": 1,
                "VISIBLE_NO_DELETE": 2,
                "VISIBLE_STALE_DELETE_IGNORED": 1,
            },
        )

    def test_decision_chain_and_equal_sequence(self):
        decisions, _ = compute_visibility(self.rows, self.data, self.ops)
        by_id = {item.row_id: item for item in decisions}
        steps = [item["step"] for item in by_id["A"].chain]
        self.assertEqual(
            steps,
            [
                "initial_visibility",
                "position_delete",
                "equality_delete",
                "final_visibility",
            ],
        )
        self.assertEqual(by_id["A"].chain[-1]["visible"], False)
        # f_new 的 sequence=4 与等值删除 sequence=4 相等，默认应用删除。
        self.assertIn("eq_a", by_id["C"].equality_hits)
        # seq=1 的等值删除只对 f_old(seq=2) 也不适用。
        self.assertNotIn("eq_old", by_id["B"].equality_hits)

    def test_visibility_by_file(self):
        decisions, _ = compute_visibility(self.rows, self.data, self.ops)
        summary = visibility_by_file(decisions)
        self.assertEqual(
            summary,
            [
                {"file_id": "f_new", "rows": 3, "visible": 2, "hidden": 1},
                {"file_id": "f_old", "rows": 2, "visible": 1, "hidden": 1},
            ],
        )

    def test_stale_chain_marks_ignored(self):
        decisions, _ = compute_visibility(self.rows, self.data, self.ops)
        chain = {item.row_id: item for item in decisions}["D"].chain
        self.assertIn("stale_delete_ignored", [item["step"] for item in chain])
        self.assertTrue(chain[-1]["visible"])


if __name__ == "__main__":
    unittest.main()
