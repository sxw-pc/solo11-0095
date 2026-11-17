import json
from pathlib import Path
import unittest

from lakedel.engine import execute_audit
from lakedel.incident_handoff_intake import static_preview


ROOT = Path(__file__).resolve().parents[1]


class IncidentHandoffIntakeTests(unittest.TestCase):
    def test_static_preview_binds_to_file_level_orphan_finding(self):
        data = json.loads((ROOT / "examples/incident/intake.json").read_text(encoding="utf-8"))
        result = execute_audit(ROOT / "examples/incident/lake")
        report = static_preview(data, result)
        self.assertEqual(report["finding_code"], "ORPHAN_DELETE_TARGET")
        self.assertEqual(report["static_intent"], {"kind": "PAGE_PRIMARY", "recipient": "reviewer-primary"})
        self.assertEqual(report["queued_event_count"], 5)
        self.assertEqual(result.stats["diffs"], 0)

    def test_fixture_contains_duplicate_timer_and_late_primary_ack(self):
        data = json.loads((ROOT / "examples/incident/intake.json").read_text(encoding="utf-8"))
        events = data["events"]
        self.assertEqual([event["event_id"] for event in events].count("timer-1"), 2)
        primary = next(event for event in events if event["event_id"] == "primary-ack")
        self.assertLess(primary["occurred_at"], "2024-06-01T09:05:00Z")
        self.assertGreater(primary["received_at"], "2024-06-01T09:05:00Z")
        self.assertEqual(next(event for event in events if event["event_id"] == "old-ack")["run_key"], "old-audit")
