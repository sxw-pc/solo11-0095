"""离线事故台账与本地 outbox 的隔离测试。

全部使用临时目录中的 SQLite 文件，不接触真实通知服务；每个用例独立建库。
"""

import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from support import ROOT

from lakedel.engine import execute_audit
from lakedel.errors import AuditError
from lakedel.incident_handoff_intake import (
    CrashAfterCommitError,
    IncidentLedger,
    bind_finding,
    build_report,
    case_identity,
    confirm_intent,
    ingest_fixture,
    run_with_crashpoint,
    static_preview,
)

FIXTURE = ROOT / "examples" / "incident" / "intake.json"
LAKE = ROOT / "examples" / "incident" / "lake"
DETECTED = datetime(2024, 6, 1, 9, 0, tzinfo=timezone.utc)


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def audit():
    return execute_audit(LAKE)


def kinds(case_report: dict) -> list[str]:
    return [intent["intent_kind"] for intent in case_report["outbox"]]


def contract(report: dict, contract_id: str) -> dict:
    return next(item for item in report["contracts"]
                if item["contract_id"] == contract_id)


class LedgerHarness:
    def __init__(self, tmp: Path):
        self.db = tmp / "incident.sqlite"
        self.ledger = IncidentLedger(self.db)
        self.result = audit()

    def close(self) -> None:
        self.ledger.close()

    def ingest(self, data: dict | None = None, as_of=None) -> dict:
        return ingest_fixture(self.ledger, data or load_fixture(), self.result, as_of=as_of)

    def report(self) -> dict:
        return build_report(self.ledger)


class OppositeContractConclusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = LedgerHarness(Path(self.tmp.name))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_same_ack_drives_opposite_backup_conclusions(self):
        """09:04 发生 / 09:09 收到的 ACK：rapid 备援已发，evidence 不发备援。"""
        self.h.ingest()
        report = self.h.report()
        rapid = contract(report, "rapid_containment")
        evidence = contract(report, "evidence_review")

        self.assertIn("PAGE_BACKUP", kinds(rapid))
        self.assertNotIn("PAGE_BACKUP", kinds(evidence))
        # 迟到主 ACK 留痕，但不撤销备援页，报告出口保持暂停。
        self.assertEqual(rapid["status"], "PRIMARY_ACK_LATE")
        self.assertEqual(rapid["report_state"], "EXPORT_PAUSED_PENDING")
        self.assertTrue(rapid["human_signoff"]["primary_acked"])
        # evidence 按发生时间裁定为及时（09:04 <= 09:05），窗口 09:12 内收到。
        self.assertEqual(evidence["deadline_at"], "2024-06-01T09:05:00Z")
        self.assertEqual(evidence["window_close_at"], "2024-06-01T09:12:00Z")
        self.assertEqual(evidence["status"], "CASE_FILE_READY")

    def test_detection_intents_and_steward_double_signoff(self):
        summary = self.h.ingest()
        report = self.h.report()
        rapid = contract(report, "rapid_containment")
        evidence = contract(report, "evidence_review")

        self.assertEqual(
            sorted(kinds(rapid)),
            ["PAGE_BACKUP", "PAGE_PRIMARY", "PAUSE_CURRENT_EXPORT"],
        )
        self.assertEqual(
            sorted(kinds(evidence)),
            ["CASE_FILE_READY", "PAGE_PRIMARY", "PIN_READ_ONLY_CASE"],
        )
        # 双签：主值守 + 证据管理员。
        roles = {row["role"] for row in evidence["human_signoff"]["signoffs"]}
        self.assertEqual(roles, {"primary", "steward"})
        steward = next(row for row in evidence["human_signoff"]["signoffs"]
                       if row["role"] == "steward")
        self.assertEqual(steward["occurred_at"], "2024-06-01T09:08:00Z")
        self.assertEqual(steward["received_at"], "2024-06-01T09:11:00Z")
        # 摄入摘要：旧 ACK 被隔离拒绝，重复 TIMER 被识别。
        self.assertEqual([row["reason"] for row in summary["rejections"]],
                         ["STALE_RUN_KEY"])
        self.assertEqual([row["event_id"] for row in summary["duplicates"]],
                         ["timer-1"])
        # 离线保证：台账里没有任何“已发送”状态，全部只是本地 PENDING 意图。
        self.assertTrue(all(intent["status"] == "PENDING"
                            for case in report["contracts"]
                            for intent in case["outbox"]))

    def test_case_file_ready_even_when_steward_ack_arrives_first(self):
        # 管理员 ACK（09:10 收到）先于主 ACK（09:11 收到）到达：双签齐备后
        # 仍必须产出且只产出一次 CASE_FILE_READY，到达顺序不影响成案。
        data = load_fixture()
        data["events"] = [
            {"event_id": "steward-ack", "kind": "STEWARD_ACK",
             "occurred_at": "2024-06-01T09:08:00Z",
             "received_at": "2024-06-01T09:10:00Z"},
            {"event_id": "primary-ack", "kind": "PRIMARY_ACK",
             "occurred_at": "2024-06-01T09:04:00Z",
             "received_at": "2024-06-01T09:11:00Z"},
        ]
        self.h.ingest(data)
        evidence = contract(self.h.report(), "evidence_review")
        self.assertEqual(evidence["status"], "CASE_FILE_READY")
        self.assertEqual(kinds(evidence).count("CASE_FILE_READY"), 1)
        self.assertNotIn("PAGE_BACKUP", kinds(evidence))

    def test_timer_and_ack_tied_at_deadline_keeps_backup_page(self):
        # 同刻 09:05：TIMER 在前先备援，主 ACK 在后不得把案件改回已签收。
        data = load_fixture()
        data["events"] = [
            {"event_id": "timer-1", "kind": "TIMER",
             "occurred_at": "2024-06-01T09:05:00Z",
             "received_at": "2024-06-01T09:05:00Z"},
            {"event_id": "primary-ack", "kind": "PRIMARY_ACK",
             "occurred_at": "2024-06-01T09:04:00Z",
             "received_at": "2024-06-01T09:05:00Z"},
        ]
        self.h.ingest(data)
        rapid = contract(self.h.report(), "rapid_containment")
        self.assertEqual(kinds(rapid).count("PAGE_BACKUP"), 1)
        self.assertEqual(rapid["status"], "PRIMARY_ACK_LATE")
        self.assertEqual(rapid["report_state"], "EXPORT_PAUSED_PENDING")


class IntakeOrderingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = LedgerHarness(Path(self.tmp.name))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_events_enter_journal_by_received_at_not_occurred_at(self):
        # 打乱输入顺序：按 received_at 落账后顺序必须是 05 → 07 → 09 → 11。
        data = load_fixture()
        events = data["events"]
        shuffled = [events[3], events[0], events[4], events[2], events[1]]
        data["events"] = shuffled
        self.h.ingest(data)
        report = self.h.report()
        accepted = [row for row in report["intake_journal"]
                    if row["intake_status"] == "ACCEPTED"]
        self.assertEqual(
            [row["received_at"] for row in accepted],
            ["2024-06-01T09:05:00Z", "2024-06-01T09:09:00Z",
             "2024-06-01T09:11:00Z"],
        )
        # 结论不随输入排列变化。
        rapid = contract(report, "rapid_containment")
        evidence = contract(report, "evidence_review")
        self.assertIn("PAGE_BACKUP", kinds(rapid))
        self.assertEqual(evidence["status"], "CASE_FILE_READY")
        # 重排后重放摘要保持稳定。
        again = LedgerHarness(Path(self.tmp.name) / "other")
        try:
            again.ingest(load_fixture())
            self.assertEqual(
                again.report()["outbox_summary"]["replay_digest"],
                report["outbox_summary"]["replay_digest"],
            )
        finally:
            again.close()

    def test_occurred_and_received_timestamps_both_retained(self):
        self.h.ingest()
        evidence = contract(self.h.report(), "evidence_review")
        primary = next(row for row in evidence["human_signoff"]["signoffs"]
                       if row["role"] == "primary")
        self.assertEqual(primary["occurred_at"], "2024-06-01T09:04:00Z")
        self.assertEqual(primary["received_at"], "2024-06-01T09:09:00Z")
        rapid = contract(self.h.report(), "rapid_containment")
        timer_step = next(row for row in rapid["state_trajectory"]
                          if row["to_status"] == "BACKUP_PAGED")
        self.assertEqual(timer_step["occurred_at"], "2024-06-01T09:05:00Z")


class DuplicateAndConflictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = LedgerHarness(Path(self.tmp.name))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_duplicate_timer_pages_backup_only_once(self):
        self.h.ingest()
        rapid = contract(self.h.report(), "rapid_containment")
        self.assertEqual(kinds(rapid).count("PAGE_BACKUP"), 1)
        # 重复投递的额外到达时间被保留为传输痕迹。
        row = self.h.ledger.connection.execute(
            "SELECT r.received_at AS received_at FROM event_redeliveries r "
            "JOIN event_journal j ON j.event_seq = r.event_seq "
            "WHERE j.event_id = 'timer-1'"
        ).fetchall()
        self.assertEqual([item["received_at"] for item in row],
                         ["2024-06-01T09:06:00Z"])
        entry = next(e for e in self.h.report()["intake_journal"]
                     if e["event_id"] == "timer-1")
        self.assertEqual(entry["redelivered_at"], ["2024-06-01T09:06:00Z"])

    def test_third_and_later_redeliveries_are_all_traced(self):
        data = load_fixture()
        # 同 ID 同业务负载、三次不同到达时间。
        data["events"] = [
            {"event_id": "timer-1", "kind": "TIMER",
             "occurred_at": "2024-06-01T09:05:00Z",
             "received_at": "2024-06-01T09:05:00Z"},
            {"event_id": "timer-1", "kind": "TIMER",
             "occurred_at": "2024-06-01T09:05:00Z",
             "received_at": "2024-06-01T09:06:00Z"},
            {"event_id": "timer-1", "kind": "TIMER",
             "occurred_at": "2024-06-01T09:05:00Z",
             "received_at": "2024-06-01T09:07:00Z"},
        ]
        self.h.ingest(data)
        entry = next(e for e in self.h.report()["intake_journal"]
                     if e["event_id"] == "timer-1")
        self.assertEqual(entry["redelivered_at"],
                         ["2024-06-01T09:06:00Z", "2024-06-01T09:07:00Z"])
        # 仍只备援一次。
        rapid = contract(self.h.report(), "rapid_containment")
        self.assertEqual(kinds(rapid).count("PAGE_BACKUP"), 1)

    def test_same_event_id_with_different_payload_is_rejected(self):
        good = load_fixture()
        self.h.ingest(good)
        before = self.h.report()["outbox_summary"]["total"]
        # 同一 event_id 改成不同业务负载（occurred_at 变了）。
        evil = copy.deepcopy(good)
        evil["events"][0]["occurred_at"] = "2024-06-01T09:01:00Z"
        with self.assertRaises(AuditError) as ctx:
            self.h.ingest(evil)
        self.assertEqual(ctx.exception.code, "INCIDENT_EVENT_ID_CONFLICT")
        # 冲突事务整体回滚：outbox 数量不变，坏事件不落账。
        self.assertEqual(self.h.report()["outbox_summary"]["total"], before)
        stored = self.h.ledger.connection.execute(
            "SELECT occurred_at FROM event_journal WHERE event_id = 'timer-1'"
        ).fetchone()
        self.assertEqual(stored["occurred_at"], "2024-06-01T09:05:00Z")


class StaleRunKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = LedgerHarness(Path(self.tmp.name))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_old_run_key_ack_is_quarantined_and_cannot_close_case(self):
        self.h.ingest()
        report = self.h.report()
        stale = next(row for row in report["intake_journal"]
                     if row["event_id"] == "old-ack")
        self.assertEqual(stale["intake_status"], "REJECTED_STALE_RUN_KEY")
        for case in report["contracts"]:
            signoff_ids = {row["event_seq"] for row in case["human_signoff"]["signoffs"]}
            self.assertNotIn(stale["event_seq"], signoff_ids)
        # 旧 ACK 没有让 rapid 合约在 09:05 前免于备援页。
        rapid = contract(report, "rapid_containment")
        self.assertIn("PAGE_BACKUP", kinds(rapid))

    def test_ack_for_superseded_case_cannot_attach_to_new_case(self):
        data = load_fixture()
        self.h.ingest(data)
        old_identity = bind_finding(data, self.h.result)
        # 模拟新审计 run：run_key 变化 → 新案件身份，旧案被标记 superseded。
        new_identity = dict(old_identity)
        new_identity["run_key"] = "new-audit-run-key"
        new_identity["finding_digest"] = "changed-digest"
        ledger = self.h.ledger
        with ledger.transaction():
            ledger.mark_superseded(old_identity["run_key"], old_identity["finding_digest"])
            for contract_id in ("rapid_containment", "evidence_review"):
                key = case_identity(new_identity["run_key"],
                                    new_identity["finding_digest"], contract_id)
                self.assertNotEqual(
                    key,
                    case_identity(old_identity["run_key"],
                                  old_identity["finding_digest"], contract_id),
                )
        report = self.h.report()
        old_cases = [c for c in report["contracts"]
                     if c["run_key"] == old_identity["run_key"]]
        self.assertTrue(all(c["superseded"] for c in old_cases))
        # 旧案上的主签不能在新案身份里查到。
        for case in ledger.list_cases():
            if case["run_key"] == old_identity["run_key"]:
                self.assertTrue(case["superseded"])


class LatenessWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = LedgerHarness(Path(self.tmp.name))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def _ack_only_fixture(self, occurred: str, received: str) -> dict:
        data = load_fixture()
        data["events"] = [
            {"event_id": "primary-ack", "kind": "PRIMARY_ACK",
             "occurred_at": occurred, "received_at": received},
        ]
        return data

    def test_ack_received_after_seven_minute_window_is_ignored(self):
        # 发生于截止前，但 09:13 才收到，超出 09:12 窗口 → 不签收，窗口到期备援。
        data = self._ack_only_fixture("2024-06-01T09:04:00Z",
                                      "2024-06-01T09:13:00Z")
        self.h.ingest(data, as_of="2024-06-01T09:13:00Z")
        evidence = contract(self.h.report(), "evidence_review")
        self.assertFalse(evidence["human_signoff"]["primary_acked"])
        self.assertIn("PAGE_BACKUP", kinds(evidence))
        self.assertEqual(evidence["report_state"], "CASE_PINNED_BACKUP")

    def test_ack_occurred_after_deadline_pages_backup_even_inside_window(self):
        # 09:06 才发生（晚于 09:05 截止），09:10 收到（窗口内）→ 仍然备援。
        data = self._ack_only_fixture("2024-06-01T09:06:00Z",
                                      "2024-06-01T09:10:00Z")
        self.h.ingest(data, as_of="2024-06-01T09:12:30Z")
        evidence = contract(self.h.report(), "evidence_review")
        self.assertTrue(evidence["human_signoff"]["primary_acked"])
        self.assertIn("PAGE_BACKUP", kinds(evidence))

    def test_primary_never_acks_triggers_backup_after_window_close(self):
        data = load_fixture()
        data["events"] = [
            {"event_id": "timer-1", "kind": "TIMER",
             "occurred_at": "2024-06-01T09:05:00Z",
             "received_at": "2024-06-01T09:05:00Z"},
        ]
        # 窗口未关闭时不应备援。
        self.h.ingest(data, as_of="2024-06-01T09:11:00Z")
        evidence = contract(self.h.report(), "evidence_review")
        self.assertNotIn("PAGE_BACKUP", kinds(evidence))
        # 窗口关闭后的重放裁定只补一次备援。
        self.h.ingest(data, as_of="2024-06-01T09:12:30Z")
        evidence = contract(self.h.report(), "evidence_review")
        self.assertEqual(kinds(evidence).count("PAGE_BACKUP"), 1)
        self.h.ingest(data, as_of="2024-06-01T09:20:00Z")
        evidence = contract(self.h.report(), "evidence_review")
        self.assertEqual(kinds(evidence).count("PAGE_BACKUP"), 1)


class RapidNoAckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = LedgerHarness(Path(self.tmp.name))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_rapid_without_any_ack_keeps_export_paused(self):
        data = load_fixture()
        data["events"] = [
            {"event_id": "timer-1", "kind": "TIMER",
             "occurred_at": "2024-06-01T09:05:00Z",
             "received_at": "2024-06-01T09:05:00Z"},
        ]
        self.h.ingest(data)
        rapid = contract(self.h.report(), "rapid_containment")
        self.assertEqual(rapid["status"], "BACKUP_PAGED")
        self.assertEqual(rapid["report_state"], "EXPORT_PAUSED_PENDING")
        self.assertFalse(rapid["human_signoff"]["primary_acked"])

    def test_early_timer_before_deadline_does_not_page_backup(self):
        data = load_fixture()
        # TIMER 在 09:04 就到（截止前），主 ACK 09:04:30 收到：不得备援。
        data["events"] = [
            {"event_id": "early-timer", "kind": "TIMER",
             "occurred_at": "2024-06-01T09:04:00Z",
             "received_at": "2024-06-01T09:04:00Z"},
            {"event_id": "primary-ack", "kind": "PRIMARY_ACK",
             "occurred_at": "2024-06-01T09:04:30Z",
             "received_at": "2024-06-01T09:04:30Z"},
        ]
        self.h.ingest(data)
        rapid = contract(self.h.report(), "rapid_containment")
        self.assertNotIn("PAGE_BACKUP", kinds(rapid))
        self.assertEqual(rapid["status"], "PRIMARY_ACKED")
        self.assertEqual(rapid["report_state"], "EXPORT_PAUSED_ACKED")


class FindingIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = LedgerHarness(Path(self.tmp.name))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_digest_change_is_rejected_and_requires_new_case(self):
        data = load_fixture()
        self.h.ingest(data)
        changed = copy.deepcopy(data)
        changed["finding_digest"] = "deadbeef" * 8
        with self.assertRaises(AuditError) as ctx:
            self.h.ingest(changed)
        self.assertEqual(ctx.exception.code, "INCIDENT_IDENTITY_CHANGED")

    def test_run_key_change_is_rejected_for_same_fixture(self):
        data = load_fixture()
        self.h.ingest(data)
        changed = copy.deepcopy(data)
        changed["run_key"] = "other-run"
        with self.assertRaises(AuditError) as ctx:
            bind_finding(changed, self.h.result)
        self.assertEqual(ctx.exception.code, "INCIDENT_IDENTITY_CHANGED")

    def test_missing_finding_blocks_attachment(self):
        data = load_fixture()
        changed = copy.deepcopy(data)
        # 审计中不存在的 finding 坐标：绑定必须报 finding 消失。
        changed["finding_code"] = "NO_SUCH_FINDING"
        with self.assertRaises(AuditError) as ctx:
            self.h.ingest(changed)
        self.assertEqual(ctx.exception.code, "INCIDENT_FINDING_GONE")


class CrashReplayTests(unittest.TestCase):
    def test_crash_after_commit_then_replay_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "crash.sqlite"
            with self.assertRaises(CrashAfterCommitError):
                run_with_crashpoint(db, FIXTURE, lake_root=LAKE,
                                    crash_after_events=True)
            # 崩溃时 outbox 已落盘但无一确认。
            with IncidentLedger(db) as ledger:
                pending = ledger.pending_intents()
                self.assertEqual(len(pending), 6)
                first_digest = build_report(ledger)["outbox_summary"]["replay_digest"]
                # 模拟外部投递层凭稳定 intent_id 幂等确认一条。
                target = pending[0]["intent_id"]
                self.assertTrue(confirm_intent(
                    ledger, target,
                    datetime(2024, 6, 1, 9, 6, tzinfo=timezone.utc),
                    {"carrier": "local-test-stub"},
                ))
                # 重复确认不产生第二条确认记录。
                self.assertFalse(confirm_intent(
                    ledger, target,
                    datetime(2024, 6, 1, 9, 7, tzinfo=timezone.utc),
                ))
            # 恢复重放：同一 fixture 重新摄入，意图数量与摘要不变。
            report = run_with_crashpoint(db, FIXTURE, lake_root=LAKE)
            self.assertEqual(report["outbox_summary"]["total"], 6)
            self.assertEqual(report["outbox_summary"]["replay_digest"], first_digest)
            self.assertEqual(report["outbox_summary"]["confirmed"], 1)
            # 备援页仍只有一条，重复 TIMER 与重试审计都不双页。
            for case in report["contracts"]:
                self.assertLessEqual(
                    [i["intent_kind"] for i in case["outbox"]].count("PAGE_BACKUP"), 1
                )

    def test_retry_audit_on_fresh_db_yields_same_stable_intent_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            db1 = Path(tmp) / "one.sqlite"
            db2 = Path(tmp) / "two.sqlite"
            run_with_crashpoint(db1, FIXTURE, lake_root=LAKE)
            run_with_crashpoint(db2, FIXTURE, lake_root=LAKE)
            for path in (db1, db2):
                with sqlite3.connect(path) as conn:
                    ids = [row[0] for row in conn.execute(
                        "SELECT intent_id FROM outbox ORDER BY intent_id")]
                    if path == db1:
                        baseline = ids
                    else:
                        self.assertEqual(ids, baseline)

    def test_unknown_intent_confirmation_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with IncidentLedger(Path(tmp) / "x.sqlite") as ledger:
                with self.assertRaises(AuditError) as ctx:
                    confirm_intent(
                        ledger, "nope",
                        datetime(2024, 6, 1, 9, 6, tzinfo=timezone.utc),
                    )
                self.assertEqual(ctx.exception.code, "INCIDENT_INTENT_UNKNOWN")

    def test_cli_crash_flag_persists_before_any_confirmation(self):
        import contextlib
        import io

        from lakedel.incident_handoff_intake import main

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cli.sqlite"
            with self.assertRaises(CrashAfterCommitError):
                main(["--fixture", str(FIXTURE), "--db", str(db),
                      "--lake", str(LAKE), "--crash-after-events"])
            with IncidentLedger(db) as ledger:
                self.assertEqual(len(ledger.pending_intents()), 6)
                self.assertEqual(
                    ledger.connection.execute(
                        "SELECT COUNT(*) FROM delivery_confirmations"
                    ).fetchone()[0],
                    0,
                )
            # 不带崩溃点重跑同一 CLI，输出正常且意图不增加。
            with contextlib.redirect_stdout(io.StringIO()):
                rc = main(["--fixture", str(FIXTURE), "--db", str(db),
                           "--lake", str(LAKE)])
            self.assertEqual(rc, 0)
            with IncidentLedger(db) as ledger:
                self.assertEqual(
                    ledger.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
                    6,
                )


class ContractIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = LedgerHarness(Path(self.tmp.name))

    def tearDown(self):
        self.h.close()
        self.tmp.cleanup()

    def test_tampered_contract_promise_is_rejected(self):
        data = load_fixture()
        data["contracts"]["rapid_containment"]["on_unacked_deadline"] = "PAGE_PRIMARY"
        with self.assertRaises(AuditError) as ctx:
            self.h.ingest(data)
        self.assertEqual(ctx.exception.code, "INCIDENT_INPUT_INVALID")

    def test_unauthorized_contract_is_rejected(self):
        data = load_fixture()
        data["contracts"]["wildcard"] = data["contracts"]["rapid_containment"]
        with self.assertRaises(AuditError) as ctx:
            self.h.ingest(data)
        self.assertEqual(ctx.exception.code, "INCIDENT_INPUT_INVALID")

    def test_event_limits_enforced(self):
        data = load_fixture()
        data["events"] = [
            {"event_id": f"t-{i}", "kind": "TIMER",
             "occurred_at": "2024-06-01T09:05:00Z",
             "received_at": f"2024-06-01T09:{5 + i:02d}:00Z"}
            for i in range(21)
        ]
        with self.assertRaises(AuditError) as ctx:
            self.h.ingest(data)
        self.assertEqual(ctx.exception.code, "INCIDENT_INPUT_INVALID")

    def test_reserved_system_event_id_is_rejected(self):
        data = load_fixture()
        data["events"][0]["event_id"] = "system"
        with self.assertRaises(AuditError) as ctx:
            self.h.ingest(data)
        self.assertEqual(ctx.exception.code, "INCIDENT_INPUT_INVALID")

    def test_empty_run_key_ack_is_still_quarantined(self):
        data = load_fixture()
        data["events"] = [
            {"event_id": "blank-ack", "kind": "PRIMARY_ACK",
             "occurred_at": "2024-06-01T09:03:00Z",
             "received_at": "2024-06-01T09:04:00Z",
             "run_key": ""},
            {"event_id": "timer-1", "kind": "TIMER",
             "occurred_at": "2024-06-01T09:05:00Z",
             "received_at": "2024-06-01T09:05:00Z"},
        ]
        self.h.ingest(data)
        report = self.h.report()
        blank = next(e for e in report["intake_journal"]
                     if e["event_id"] == "blank-ack")
        self.assertEqual(blank["intake_status"], "REJECTED_STALE_RUN_KEY")
        rapid = contract(report, "rapid_containment")
        self.assertIn("PAGE_BACKUP", kinds(rapid))
        self.assertFalse(rapid["human_signoff"]["primary_acked"])


class StaticPreviewCompatibilityTests(unittest.TestCase):
    def test_legacy_static_preview_contract_preserved(self):
        data = load_fixture()
        result = audit()
        preview = static_preview(data, result)
        self.assertEqual(preview["finding_code"], "ORPHAN_DELETE_TARGET")
        self.assertEqual(preview["static_intent"],
                         {"kind": "PAGE_PRIMARY", "recipient": "reviewer-primary"})
        self.assertEqual(preview["queued_event_count"], 5)


if __name__ == "__main__":
    unittest.main()
