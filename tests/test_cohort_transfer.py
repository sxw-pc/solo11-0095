import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from fractions import Fraction
from pathlib import Path
from unittest import mock

from lakedel import cohort_transfer
from lakedel.cohort_transfer import (
    ALPHA_PER_REGION,
    evaluate_quality_contracts,
    hypergeometric_tail_leq,
    hypergeometric_upper_bound,
    inspect_deletion_cohort,
)
from lakedel.util import canonical_json


ROOT = Path(__file__).resolve().parents[1] / "examples" / "quality_transfer"
DRAW = ROOT / "draw.json"


def _copy_fixture(destination: Path) -> Path:
    shutil.copytree(ROOT, destination)
    return destination


def _refresh_snapshot_hashes(workspace: Path) -> None:
    for snap_path in sorted((workspace / "snapshots").glob("*.json")):
        snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
        for key in ("data_files", "delete_files"):
            for spec in snapshot.get(key, []):
                digest = hashlib.sha256((workspace / spec["path"]).read_bytes()).hexdigest()
                spec["sha256"] = digest
        snap_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")


def _set_expected_visibility(workspace: Path, updates: dict) -> None:
    path = workspace / "expected" / "visible.csv"
    lines = path.read_bytes().decode("utf-8").split("\r\n")
    out = [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        file_id, row_pos, row_id, visible = line.split(",")
        if int(row_pos) in updates:
            visible = "true" if updates[int(row_pos)] else "false"
        out.append(f"{file_id},{row_pos},{row_id},{visible}")
    path.write_bytes(("\r\n".join(out) + "\r\n").encode("utf-8"))


def _write_draw(directory: Path, draw: dict) -> Path:
    path = Path(directory) / "draw.json"
    path.write_text(json.dumps(draw), encoding="utf-8")
    return path


class HypergeometricWitnessTest(unittest.TestCase):
    def test_exact_inversion_matches_fixture_witness(self):
        alpha = ALPHA_PER_REGION
        self.assertEqual(alpha, Fraction(1, 40))
        self.assertEqual(hypergeometric_upper_bound(20, 10, 0, alpha), 4)
        self.assertGreaterEqual(hypergeometric_tail_leq(20, 4, 10, 0), alpha)
        self.assertLess(hypergeometric_tail_leq(20, 5, 10, 0), alpha)
        self.assertEqual(hypergeometric_tail_leq(20, 4, 10, 0), Fraction(14, 323))
        self.assertEqual(hypergeometric_tail_leq(20, 5, 10, 0), Fraction(21, 1292))
        self.assertEqual(hypergeometric_upper_bound(20, 10, 1, alpha), 7)

    def test_invalid_parameters_rejected(self):
        with self.assertRaisesRegex(ValueError, "out of range"):
            hypergeometric_upper_bound(10, 20, 0, Fraction(1, 40))
        with self.assertRaisesRegex(ValueError, "alpha"):
            hypergeometric_upper_bound(20, 10, 0, Fraction(0))
        with self.assertRaisesRegex(ValueError, "out of range"):
            hypergeometric_tail_leq(10, 11, 5, 1)


class FixtureDecisionTest(unittest.TestCase):
    def test_portable_publishes_and_fresh_holds(self):
        result = inspect_deletion_cohort(ROOT, DRAW)
        self.assertEqual(result["population_before_by_region"], {"east": 20, "west": 20})
        self.assertEqual(result["population_after_by_region"], {"east": 15, "west": 20})
        self.assertEqual(result["sample_by_region"], {"east": 10, "west": 10})
        self.assertEqual(result["deleted_inspected_count"], 0)
        self.assertEqual(result["surviving_inspected_count"], 20)
        self.assertEqual(result["sample_defects_by_region"], {})

        bounds = result["population_upper_bounds"]["unconditional_portable"]
        self.assertEqual(bounds["defects_bound"], 8)
        self.assertEqual(bounds["population"], 35)
        self.assertEqual(bounds["rate"], "8/35")
        self.assertEqual(bounds["rate_decimal"], "0.228571428571")
        east = bounds["regions"]["east"]
        self.assertEqual(east["population_before"], 20)
        self.assertEqual(east["sample_size"], 10)
        self.assertEqual(east["observed_defects"], 0)
        self.assertEqual(east["alpha"], "1/40")
        self.assertEqual(east["defect_bound_before"], 4)
        self.assertEqual(east["confirmed_deleted_defects"], 0)
        self.assertEqual(east["carried_bound_after"], 4)
        self.assertEqual(east["surviving_population"], 15)
        self.assertEqual(east["tail_at_bound"], "14/323")
        self.assertEqual(east["tail_above_bound"], "21/1292")
        west = bounds["regions"]["west"]
        self.assertEqual(west["defect_bound_before"], 4)
        self.assertEqual(west["surviving_population"], 20)
        self.assertIsNone(result["population_upper_bounds"]["fresh_current_draw"])

        gate = result["publication_decision"]["gate"]
        self.assertEqual(gate["confidence"], "19/20")
        self.assertEqual(gate["threshold"], "1/4")
        contracts = result["publication_decision"]["contracts"]

        portable = contracts["unconditional_portable"]
        self.assertEqual(portable["status"], "publish")
        certificate = portable["certificate"]
        self.assertEqual(certificate["contract"], "unconditional_portable")
        self.assertIn("unconditional coverage", certificate["guarantee_basis"])
        self.assertIn("pre-deletion", certificate["guarantee_basis"])
        self.assertEqual(certificate["before_run_key"], result["before_run_key"])
        self.assertEqual(certificate["after_run_key"], result["after_run_key"])
        self.assertEqual(certificate["population_after"], 35)
        self.assertEqual(certificate["defects_bound"], 8)
        self.assertEqual(certificate["rate_bound"], "8/35")
        self.assertEqual(certificate["simultaneous_coverage"], "19/20")
        self.assertEqual(certificate["alpha_by_region"], {"east": "1/40", "west": "1/40"})
        self.assertEqual(certificate["region_witness"], bounds["regions"])
        self.assertIn("not asserted to be the unique tightest", certificate["bound_nature"])

        fresh = contracts["fresh_current_draw"]
        self.assertEqual(fresh["status"], "hold")
        self.assertIsNone(fresh["certificate"])
        self.assertTrue(any("fresh random draw" in reason for reason in fresh["reasons"]))
        self.assertTrue(any("evidence timing only" in reason for reason in fresh["reasons"]))
        self.assertFalse(any("above gate" in reason for reason in fresh["reasons"]))

    def test_replay_is_byte_stable(self):
        first = inspect_deletion_cohort(ROOT, DRAW)
        second = inspect_deletion_cohort(ROOT, DRAW)
        self.assertEqual(first, second)
        self.assertEqual(canonical_json(first), canonical_json(second))

    def test_tightened_gate_blocks_legitimate_bound_not_wrong_rebase(self):
        result = inspect_deletion_cohort(ROOT, DRAW, Fraction(1, 5))
        bounds = result["population_upper_bounds"]["unconditional_portable"]
        self.assertEqual(bounds["defects_bound"], 8)
        portable = result["publication_decision"]["contracts"]["unconditional_portable"]
        self.assertEqual(portable["status"], "hold")
        self.assertIsNone(portable["certificate"])
        self.assertTrue(any("above gate 1/5" in reason for reason in portable["reasons"]))

        # The erroneous re-base: surviving 10 inspected east rows misread as a
        # fresh SRSWOR of the N=15 surviving east frame would pass the same gate.
        wrong_east = hypergeometric_upper_bound(15, 10, 0, ALPHA_PER_REGION)
        wrong_west = hypergeometric_upper_bound(20, 10, 0, ALPHA_PER_REGION)
        self.assertEqual((wrong_east, wrong_west), (2, 4))
        self.assertLessEqual(Fraction(wrong_east + wrong_west, 35), Fraction(1, 5))
        # The legitimate carried bound keeps the pre-deletion frame and blocks.
        self.assertGreater(Fraction(bounds["defects_bound"], 35), Fraction(1, 5))
        self.assertEqual(bounds["regions"]["east"]["population_before"], 20)
        self.assertEqual(bounds["regions"]["east"]["surviving_population"], 15)

    def test_defect_receipt_change_recomputes_and_blocks(self):
        baseline = inspect_deletion_cohort(ROOT, DRAW)
        draw = json.loads(DRAW.read_text(encoding="utf-8"))
        receipt = next(item for item in draw["inspections"] if item["row_id"] == "Q0004")
        receipt["defect"] = True
        with tempfile.TemporaryDirectory() as tmp:
            result = inspect_deletion_cohort(ROOT, _write_draw(Path(tmp), draw))
        bounds = result["population_upper_bounds"]["unconditional_portable"]
        self.assertEqual(bounds["regions"]["east"]["observed_defects"], 1)
        self.assertEqual(bounds["regions"]["east"]["defect_bound_before"], 7)
        self.assertEqual(bounds["defects_bound"], 11)
        self.assertEqual(bounds["rate"], "11/35")
        portable = result["publication_decision"]["contracts"]["unconditional_portable"]
        self.assertEqual(portable["status"], "hold")
        self.assertIsNone(portable["certificate"])
        self.assertNotEqual(result["evidence_sha256"], baseline["evidence_sha256"])

    def test_changed_draw_or_snapshot_binding_rejected(self):
        draw = json.loads(DRAW.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draw.json"
            draw["inspections"][0]["row_id"] = "Q0001"
            path.write_text(json.dumps(draw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fixed random draw"):
                inspect_deletion_cohort(ROOT, path)
            draw = json.loads(DRAW.read_text(encoding="utf-8"))
            draw["after_snapshot"] = "wrong"
            path.write_text(json.dumps(draw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "snapshot binding"):
                inspect_deletion_cohort(ROOT, path)


class CounterExampleTest(unittest.TestCase):
    def test_deleted_inspected_row_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_fixture(Path(tmp) / "case")
            deletes = root / "after" / "deletes" / "post_sample.jsonl"
            lines = deletes.read_bytes().decode("utf-8").split("\r\n")
            lines = [line.replace('"row_pos": 5', '"row_pos": 3') for line in lines]
            deletes.write_bytes("\r\n".join(lines).encode("utf-8"))
            _set_expected_visibility(root / "after", {3: False, 5: True})
            _refresh_snapshot_hashes(root / "after")
            with self.assertRaisesRegex(ValueError, "post-draw policy"):
                inspect_deletion_cohort(root, DRAW)

    def test_added_rows_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_fixture(Path(tmp) / "case")
            data = root / "after" / "data" / "quality.csv"
            with data.open("a", encoding="utf-8", newline="") as handle:
                handle.write("Q0041,40,quality,east,140,2024-06-01\r\n")
            expected = root / "after" / "expected" / "visible.csv"
            with expected.open("a", encoding="utf-8", newline="") as handle:
                handle.write("quality,40,Q0041,true\r\n")
            snap_path = root / "after" / "snapshots" / "after.json"
            snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
            snapshot["data_files"][0]["row_count"] = 41
            snap_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
            _refresh_snapshot_hashes(root / "after")
            with self.assertRaisesRegex(ValueError, "strict subset"):
                inspect_deletion_cohort(root, DRAW)

    def test_surviving_content_change_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_fixture(Path(tmp) / "case")
            data = root / "after" / "data" / "quality.csv"
            text = data.read_bytes().decode("utf-8")
            changed = text.replace(
                "Q0010,9,quality,east,109,2024-06-01",
                "Q0010,9,quality,east,999,2024-06-01",
            )
            self.assertNotEqual(changed, text)
            data.write_bytes(changed.encode("utf-8"))
            _refresh_snapshot_hashes(root / "after")
            with self.assertRaisesRegex(ValueError, "surviving row content changed"):
                inspect_deletion_cohort(root, DRAW)

    def test_deletion_before_sampling_rejected(self):
        # The before frame already hides three east rows: the receipts can no
        # longer reproduce from the audited pre-deletion frame they claim.
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_fixture(Path(tmp) / "case")
            before = root / "before"
            (before / "deletes").mkdir(exist_ok=True)
            early = before / "deletes" / "early.jsonl"
            early.write_bytes(
                "".join(
                    f'{{"file_id": "quality", "row_pos": {pos}, "reason": "early deletion"}}\r\n'
                    for pos in (0, 1, 2)
                ).encode("utf-8")
            )
            snap_path = before / "snapshots" / "before.json"
            snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
            snapshot["delete_files"] = [
                {
                    "file_id": "early",
                    "path": "deletes/early.jsonl",
                    "delete_kind": "position",
                    "sequence_number": 1,
                    "target_file_id": "quality",
                    "schema_hash": snapshot["schema_hash"],
                    "sha256": hashlib.sha256(early.read_bytes()).hexdigest(),
                }
            ]
            snap_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
            _set_expected_visibility(before, {0: False, 1: False, 2: False})
            _refresh_snapshot_hashes(before)
            with self.assertRaisesRegex(ValueError, "fixed random draw"):
                inspect_deletion_cohort(root, DRAW)

    def test_unclean_audit_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _copy_fixture(Path(tmp) / "case")
            _set_expected_visibility(root / "after", {6: False})
            with self.assertRaisesRegex(ValueError, "not clean"):
                inspect_deletion_cohort(root, DRAW)


class ContractLogicTest(unittest.TestCase):
    def _evidence(self, **overrides):
        inspections = [
            {"row_id": f"Q{i:04d}", "region": "east" if i <= 10 else "west", "defect": False}
            for i in range(1, 21)
        ]
        evidence = {
            "design": "srswor_by_region_before_deletion",
            "before_snapshot": "before",
            "after_snapshot": "after",
            "before_run_key": "b" * 64,
            "after_run_key": "a" * 64,
            "population_before_by_region": {"east": 20, "west": 20},
            "population_after_by_region": {"east": 15, "west": 20},
            "sample_by_region": {"east": 10, "west": 10},
            "sample_defects_by_region": {},
            "deleted_row_ids": [],
            "inspections": inspections,
        }
        evidence.update(overrides)
        return evidence

    def test_confirmed_deleted_defects_deducted_only_with_receipt_evidence(self):
        inspections = [
            {"row_id": "Q0001", "region": "east", "defect": True},
            {"row_id": "Q0002", "region": "east", "defect": False},
        ]
        base = dict(
            population_before_by_region={"east": 20},
            population_after_by_region={"east": 19},
            sample_by_region={"east": 10},
            sample_defects_by_region={"east": 1},
            inspections=inspections,
        )
        # Deleted row carries a defective receipt: the carried bound drops by one.
        deducted = evaluate_quality_contracts(
            self._evidence(**base, deleted_row_ids=["Q0001"])
        )
        east = deducted["population_upper_bounds"]["unconditional_portable"]["regions"]["east"]
        self.assertEqual(east["defect_bound_before"], 7)
        self.assertEqual(east["confirmed_deleted_defects"], 1)
        self.assertEqual(east["carried_bound_after"], 6)
        # Deleted row was inspected but not defective: no deduction.
        kept = evaluate_quality_contracts(
            self._evidence(**base, deleted_row_ids=["Q0002"])
        )
        east = kept["population_upper_bounds"]["unconditional_portable"]["regions"]["east"]
        self.assertEqual(east["confirmed_deleted_defects"], 0)
        self.assertEqual(east["carried_bound_after"], 7)

    def test_contracts_reject_ineligible_design(self):
        evidence = self._evidence(design="srswor_by_region_after_visible")
        contracts = evaluate_quality_contracts(evidence)["publication_decision"]["contracts"]
        portable = contracts["unconditional_portable"]
        self.assertEqual(portable["status"], "reject")
        self.assertIsNone(portable["certificate"])
        self.assertTrue(any("pre-deletion" in reason for reason in portable["reasons"]))
        fresh = contracts["fresh_current_draw"]
        self.assertEqual(fresh["status"], "reject")
        self.assertIsNone(fresh["certificate"])
        self.assertTrue(any("post-formation" in reason for reason in fresh["reasons"]))

    def test_invalid_threshold_rejected(self):
        with self.assertRaisesRegex(ValueError, "threshold"):
            inspect_deletion_cohort(ROOT, DRAW, Fraction(0))
        with self.assertRaisesRegex(ValueError, "threshold"):
            inspect_deletion_cohort(ROOT, DRAW, Fraction(1))


class ModuleCliTest(unittest.TestCase):
    def _invoke(self, *extra):
        argv = ["cohort_transfer", "--root", str(ROOT), "--draw", str(DRAW), *extra]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(out):
            cohort_transfer.main()
        return out.getvalue()

    def test_cli_outputs_both_contracts_byte_stably(self):
        output = self._invoke()
        payload = json.loads(output)
        contracts = payload["publication_decision"]["contracts"]
        self.assertEqual(contracts["unconditional_portable"]["status"], "publish")
        self.assertEqual(contracts["fresh_current_draw"]["status"], "hold")
        self.assertEqual(output, canonical_json(inspect_deletion_cohort(ROOT, DRAW)) + "\n")
        self.assertEqual(output, self._invoke())

    def test_cli_threshold_flag(self):
        payload = json.loads(self._invoke("--quality-threshold", "0.2"))
        contracts = payload["publication_decision"]["contracts"]
        self.assertEqual(contracts["unconditional_portable"]["status"], "hold")
        self.assertEqual(payload["publication_decision"]["gate"]["threshold"], "1/5")


if __name__ == "__main__":
    unittest.main()
