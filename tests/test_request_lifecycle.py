"""Request fulfillment is a separate read-only business result."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from support import ROOT

from lakedel.errors import AuditError
from lakedel.request_lifecycle import check_requests


FIXTURE = ROOT / "examples" / "request_lake"


class RequestLifecycleTest(unittest.TestCase):
    def test_pending_fulfilled_and_regressed(self):
        positive = check_requests(FIXTURE, through_snapshot="snap_002")
        self.assertEqual([t["status"] for t in positive["requests"][0]["timeline"]],
                         ["pending", "fulfilled"])
        self.assertEqual(positive["requests"][0]["status"], "fulfilled")
        negative = check_requests(FIXTURE)
        self.assertEqual(negative["requests"][0]["status"], "regressed")
        self.assertEqual(negative["requests"][0]["timeline"][-1]["visible_row_ids"],
                         ["R0001"])

    def test_deadline_breach_and_late_fulfillment(self):
        with tempfile.TemporaryDirectory() as directory:
            lake = Path(directory) / "lake"
            shutil.copytree(FIXTURE, lake)
            path = lake / "requests" / "erasure_requests.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["requests"][0]["due_at"] = "2024-06-01T12:00:00Z"
            path.write_text(json.dumps(data), encoding="utf-8")
            result = check_requests(lake, through_snapshot="snap_002")
            self.assertEqual([t["status"] for t in result["requests"][0]["timeline"]],
                             ["pending", "fulfilled_late"])

    def test_unknown_target_never_counts_as_fulfilled(self):
        with tempfile.TemporaryDirectory() as directory:
            lake = Path(directory) / "lake"
            shutil.copytree(FIXTURE, lake)
            path = lake / "requests" / "erasure_requests.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["requests"][0]["row_ids"] = ["R9999"]
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(check_requests(lake)["requests"][0]["status"],
                             "indeterminate")

    def test_untrusted_delete_evidence_prevents_fulfillment(self):
        with tempfile.TemporaryDirectory() as directory:
            lake = Path(directory) / "lake"
            shutil.copytree(FIXTURE, lake)
            path = lake / "deletes" / "pos.jsonl"
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            result = check_requests(lake, through_snapshot="snap_002")
            self.assertEqual(result["requests"][0]["status"], "indeterminate")
            self.assertIn("FILE_HASH_MISMATCH",
                          result["requests"][0]["timeline"][-1]["untrusted_codes"])

    def test_missing_snapshot_and_duplicate_request_are_rejected(self):
        with self.assertRaises(AuditError):
            check_requests(FIXTURE, through_snapshot="missing")
        with tempfile.TemporaryDirectory() as directory:
            lake = Path(directory) / "lake"
            shutil.copytree(FIXTURE, lake)
            path = lake / "requests" / "erasure_requests.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["requests"].append(data["requests"][0])
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(AuditError):
                check_requests(lake)


if __name__ == "__main__":
    unittest.main()
