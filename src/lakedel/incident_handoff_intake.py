"""离线事故通知与交接：持久事故台账、双合约状态机与本地 outbox。

本模块只产出本地意图（outbox 行），从不连接真实寻呼/通知服务。所有状态
转移与待发布意图在同一个 SQLite 事务内落盘，崩溃后重放不会重复生成
PAGE_BACKUP / PAUSE_CURRENT_EXPORT / CASE_FILE_READY 等逻辑意图。

两个合约按 *授权合同*（fixture 的 contracts 节）独立运行，不是同一阈值的
宽严两版：

* ``rapid_containment`` 以 ``received_at`` 为钟：主值守五分钟响应按收到
  时间计；09:05 的 TIMER 未收到 ACK 即只产生一次 PAGE_BACKUP，迟到的主
  ACK 留痕但不撤回备援页，报告出口保持暂停。
* ``evidence_review`` 以事件 ``occurred_at`` 加 7 分钟有界迟到窗口为钟：
  在窗口内（09:12 前）收到的、发生于截止前的 ACK 视为及时，不产生备援
  页；PRIMARY_ACK 与 STEWARD_ACK 双签后产出 CASE_FILE_READY，且不对
  当前审计数据做任何写回。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .engine import execute_audit
from .errors import AuditError
from .util import canonical_json, sha256_text

# ---------------------------------------------------------------------------
# 常量与输入约束（最多 20 事件 / 2 合同 / 3 角色）
# ---------------------------------------------------------------------------

MAX_EVENTS = 20
MAX_CONTRACTS = 2
ROLES = ("primary", "backup", "steward")
CONTRACT_IDS = ("rapid_containment", "evidence_review")

# rapid_containment 合同在 fixture 中预先授权的承诺文字。
RAPID_CLAUSES = frozenset(
    {
        "clock",
        "primary_deadline_minutes",
        "on_detection",
        "on_unacked_deadline",
        "on_late_primary_ack",
    }
)
# evidence_review 合同在 fixture 中预先授权的承诺文字。
EVIDENCE_CLAUSES = frozenset(
    {
        "clock",
        "lateness_minutes",
        "on_detection",
        "require_signoffs",
        "on_complete",
        "on_timely_ack",
    }
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS cases (
  case_key TEXT PRIMARY KEY,
  case_seq INTEGER NOT NULL UNIQUE,
  run_key TEXT NOT NULL,
  finding_digest TEXT NOT NULL,
  contract_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  finding_code TEXT NOT NULL,
  finding_file TEXT NOT NULL,
  finding_row TEXT,
  contract_json TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  deadline_at TEXT,
  window_close_at TEXT,
  status TEXT NOT NULL,
  report_state TEXT NOT NULL,
  created_event_seq INTEGER NOT NULL,
  superseded INTEGER NOT NULL DEFAULT 0,
  UNIQUE(run_key, finding_digest, contract_id)
);
CREATE TABLE IF NOT EXISTS event_journal (
  event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  event_run_key TEXT,
  intake_status TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS event_redeliveries (
  event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
  received_at TEXT NOT NULL,
  PRIMARY KEY(event_seq, received_at)
);
CREATE TABLE IF NOT EXISTS case_events (
  case_key TEXT NOT NULL REFERENCES cases(case_key),
  event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
  effect TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(case_key, event_seq)
);
CREATE TABLE IF NOT EXISTS transitions (
  case_key TEXT NOT NULL REFERENCES cases(case_key),
  event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
  from_status TEXT NOT NULL,
  to_status TEXT NOT NULL,
  note TEXT NOT NULL,
  at_time TEXT NOT NULL,
  PRIMARY KEY(case_key, event_seq, to_status)
);
CREATE TABLE IF NOT EXISTS case_signoffs (
  case_key TEXT NOT NULL REFERENCES cases(case_key),
  role TEXT NOT NULL,
  event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
  occurred_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY(case_key, role)
);
CREATE TABLE IF NOT EXISTS outbox (
  intent_id TEXT PRIMARY KEY,
  case_key TEXT NOT NULL REFERENCES cases(case_key),
  event_seq INTEGER NOT NULL REFERENCES event_journal(event_seq),
  intent_kind TEXT NOT NULL,
  recipient TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING',
  confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS delivery_confirmations (
  intent_id TEXT NOT NULL PRIMARY KEY REFERENCES outbox(intent_id),
  confirmed_at TEXT NOT NULL,
  token_json TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# 时间与摘要工具
# ---------------------------------------------------------------------------

def parse_ts(value: str, label: str) -> datetime:
    """解析以 Z 结尾的 UTC 时间戳。"""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AuditError("INCIDENT_INPUT_INVALID", f"{label} 必须是以 Z 结尾的 UTC 时间")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditError("INCIDENT_INPUT_INVALID", f"{label} 不是合法时间戳: {value}") from exc
    if result.tzinfo != timezone.utc:
        raise AuditError("INCIDENT_INPUT_INVALID", f"{label} 必须是 UTC")
    return result


def fmt_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_payload_hash(event: dict[str, Any]) -> str:
    """事件*业务负载*摘要。

    received_at 是投递副本的到达时间（传输元数据）：同一事件重复投递时它
    可以不同，因此不纳入身份哈希。真正决定语义的是 kind / occurred_at /
    run_key 等业务字段——这些字段不同而 event_id 相同即为复用冲突。
    """
    payload = {
        key: value
        for key, value in event.items()
        if key not in ("event_id", "received_at") and not key.startswith("_")
    }
    return sha256_text(canonical_json(payload))


def case_identity(run_key: str, finding_digest: str, contract_id: str) -> str:
    """事故身份绑定 run_key + finding_digest + 授权合同。"""
    return sha256_text(
        canonical_json(
            {
                "run_key": run_key,
                "finding_digest": finding_digest,
                "contract": contract_id,
            }
        )
    )


# ---------------------------------------------------------------------------
# fixture 校验
# ---------------------------------------------------------------------------

def _require_role(data: dict[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise AuditError("INCIDENT_INPUT_INVALID", f"角色 {name} 必须是非空字符串")
    return value


def validate_fixture(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AuditError("INCIDENT_INPUT_INVALID", "fixture 必须是 JSON 对象")
    for field in ("snapshot_id", "run_key", "finding_code", "finding_file",
                  "finding_digest", "detected_at"):
        if not isinstance(data.get(field), str) or not data[field]:
            raise AuditError("INCIDENT_INPUT_INVALID", f"字段 {field} 缺失或非法")
    if "finding_row" not in data:
        raise AuditError("INCIDENT_INPUT_INVALID", "字段 finding_row 缺失")
    for name in ("primary", "backup", "steward"):
        _require_role(data, name)
    role_values = [data[name] for name in ROLES]
    if len(set(role_values)) != len(ROLES):
        raise AuditError("INCIDENT_INPUT_INVALID", "三个角色 primary/backup/steward 必须互不相同")
    detected_at = parse_ts(data["detected_at"], "detected_at")
    if not isinstance(data.get("primary_deadline_minutes"), int) or data[
        "primary_deadline_minutes"
    ] <= 0:
        raise AuditError("INCIDENT_INPUT_INVALID",
                         "primary_deadline_minutes 必须是正整数")
    if not isinstance(data.get("bounded_lateness_minutes"), int) or data[
        "bounded_lateness_minutes"
    ] <= 0:
        raise AuditError("INCIDENT_INPUT_INVALID",
                         "bounded_lateness_minutes 必须是正整数")
    contracts = data.get("contracts")
    if not isinstance(contracts, dict) or not contracts:
        raise AuditError("INCIDENT_INPUT_INVALID", "contracts 必须是非空对象")
    if len(contracts) > MAX_CONTRACTS:
        raise AuditError("INCIDENT_INPUT_INVALID", f"合同数不得超过 {MAX_CONTRACTS}")
    unknown = set(contracts) - set(CONTRACT_IDS)
    if unknown:
        raise AuditError("INCIDENT_INPUT_INVALID", f"未预授权的合同: {sorted(unknown)}")
    rapid = contracts.get("rapid_containment")
    if rapid is not None:
        if set(rapid) != RAPID_CLAUSES:
            raise AuditError("INCIDENT_INPUT_INVALID",
                             "rapid_containment 合同条款与预授权承诺不一致")
        if rapid["clock"] != "received_at":
            raise AuditError("INCIDENT_INPUT_INVALID", "rapid_containment 时钟承诺被篡改")
        if rapid["on_detection"] != "PAUSE_CURRENT_EXPORT":
            raise AuditError("INCIDENT_INPUT_INVALID", "rapid_containment 检测承诺被篡改")
        if rapid["on_unacked_deadline"] != "PAGE_BACKUP":
            raise AuditError("INCIDENT_INPUT_INVALID", "rapid_containment 备援承诺被篡改")
        if rapid["on_late_primary_ack"] != "KEEP_BACKUP_PAGE":
            raise AuditError("INCIDENT_INPUT_INVALID", "rapid_containment 迟到 ACK 承诺被篡改")
        if not isinstance(rapid["primary_deadline_minutes"], int) or rapid[
            "primary_deadline_minutes"
        ] <= 0:
            raise AuditError("INCIDENT_INPUT_INVALID", "primary_deadline_minutes 必须是正整数")
    evidence = contracts.get("evidence_review")
    if evidence is not None:
        if set(evidence) != EVIDENCE_CLAUSES:
            raise AuditError("INCIDENT_INPUT_INVALID",
                             "evidence_review 合同条款与预授权承诺不一致")
        if evidence["clock"] != "occurred_at_with_bounded_lateness":
            raise AuditError("INCIDENT_INPUT_INVALID", "evidence_review 时钟承诺被篡改")
        if evidence["on_detection"] != "PIN_READ_ONLY_CASE":
            raise AuditError("INCIDENT_INPUT_INVALID", "evidence_review 检测承诺被篡改")
        if evidence["on_complete"] != "CASE_FILE_READY":
            raise AuditError("INCIDENT_INPUT_INVALID", "evidence_review 完成承诺被篡改")
        if evidence["on_timely_ack"] != "NO_BACKUP_PAGE":
            raise AuditError("INCIDENT_INPUT_INVALID", "evidence_review 及时 ACK 承诺被篡改")
        if list(evidence["require_signoffs"]) != ["PRIMARY_ACK", "STEWARD_ACK"]:
            raise AuditError("INCIDENT_INPUT_INVALID", "evidence_review 签收角色承诺被篡改")
        if not isinstance(evidence["lateness_minutes"], int) or evidence["lateness_minutes"] <= 0:
            raise AuditError("INCIDENT_INPUT_INVALID", "lateness_minutes 必须是正整数")
        if evidence["lateness_minutes"] != data["bounded_lateness_minutes"]:
            raise AuditError(
                "INCIDENT_INPUT_INVALID",
                "evidence_review.lateness_minutes 与顶层 bounded_lateness_minutes 不一致",
            )
    rapid = contracts.get("rapid_containment")
    if rapid is not None and rapid["primary_deadline_minutes"] != data[
        "primary_deadline_minutes"
    ]:
        raise AuditError(
            "INCIDENT_INPUT_INVALID",
            "rapid_containment.primary_deadline_minutes 与顶层承诺不一致",
        )
    events = data.get("events")
    if not isinstance(events, list) or not events or len(events) > MAX_EVENTS:
        raise AuditError("INCIDENT_INPUT_INVALID", f"events 必须含 1..{MAX_EVENTS} 条事件")
    normalized: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise AuditError("INCIDENT_INPUT_INVALID", f"events[{index}] 必须是对象")
        event_id = event.get("event_id")
        kind = event.get("kind")
        if not isinstance(event_id, str) or not event_id:
            raise AuditError("INCIDENT_INPUT_INVALID", f"events[{index}].event_id 非法")
        if event_id == "system":
            raise AuditError("INCIDENT_INPUT_INVALID",
                             'event_id "system" 是系统裁定锚点的保留 ID，输入事件不得使用')
        if kind not in ("TIMER", "PRIMARY_ACK", "STEWARD_ACK"):
            raise AuditError("INCIDENT_INPUT_INVALID", f"events[{index}].kind 未授权: {kind}")
        occurred = parse_ts(event["occurred_at"], f"events[{index}].occurred_at")
        received = parse_ts(event["received_at"], f"events[{index}].received_at")
        if occurred > received:
            raise AuditError("INCIDENT_INPUT_INVALID",
                             f"{event_id}: occurred_at 晚于 received_at")
        run_key = event.get("run_key")
        if run_key is not None and not isinstance(run_key, str):
            raise AuditError("INCIDENT_INPUT_INVALID", f"{event_id}: run_key 必须是字符串")
        normalized.append(
            {
                "event_id": event_id,
                "kind": kind,
                "occurred_at": event["occurred_at"],
                "received_at": event["received_at"],
                "run_key": run_key,
                "_occurred": occurred,
                "_received": received,
                "_payload_hash": event_payload_hash(event),
            }
        )
    return {"detected_at": detected_at, "events": normalized,
            "contracts": contracts, "raw": data}


# ---------------------------------------------------------------------------
# 静态预览（保留既有离线契约）
# ---------------------------------------------------------------------------

def static_preview(data: dict, result) -> dict:
    if result.run_key != data["run_key"] or result.workspace.snapshot.snapshot_id != data["snapshot_id"]:
        raise ValueError("incident is bound to a different audit")
    matches = [item for item in result.findings
               if item.code == data["finding_code"] and item.file == data["finding_file"]
               and item.row == data["finding_row"]]
    if len(matches) != 1:
        raise ValueError("expected exactly one matching audit finding")
    digest = sha256_text(canonical_json(matches[0].to_dict()))
    if digest != data["finding_digest"]:
        raise ValueError("audit finding evidence changed")
    key = sha256_text(result.run_key + ":" + digest)
    return {"incident_key": key, "run_key": result.run_key,
            "finding_code": matches[0].code,
            "static_intent": {"kind": "PAGE_PRIMARY", "recipient": data["primary"]},
            "queued_event_count": len(data["events"])}


# ---------------------------------------------------------------------------
# 持久事故台账与本地 outbox
# ---------------------------------------------------------------------------

class IncidentLedger:
    """SQLite 事故台账：事件落账、状态机与 outbox 共用同一事务边界。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.db_path))
        self.connection.row_factory = sqlite3.Row
        self._tx_depth = 0
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        # event_seq=0 是案件开立/窗口到期等系统裁定的锚点，外键要求它在
        # journal 中存在；真实输入事件的 AUTOINCREMENT 序号从 1 开始。
        self.connection.execute(
            """INSERT OR IGNORE INTO event_journal(event_seq, event_id, kind, occurred_at,
                                                   received_at, payload_hash, event_run_key,
                                                   intake_status, detail)
               VALUES (0, 'system', 'SYSTEM', '', '', '', NULL, 'SYSTEM',
                       'synthetic provisioning/window-expiry anchor (not an input event)')"""
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "IncidentLedger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """每个事务同时持久化状态转移与 outbox 意图（原子单元）。

        可重入：外层用 BEGIN/COMMIT，嵌套层用 SAVEPOINT，内层失败只回滚到
        保存点，不会提前提交外层未完成的工作。
        """
        if self._tx_depth == 0:
            self._tx_depth = 1
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()
            finally:
                self._tx_depth = 0
            return
        savepoint = f"incident_sp_{self._tx_depth}"
        self.connection.execute(f"SAVEPOINT {savepoint}")
        self._tx_depth += 1
        try:
            yield self.connection
        except Exception:
            self.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            self._tx_depth -= 1
            raise
        else:
            self.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            self._tx_depth -= 1

    # -- 案件 --------------------------------------------------------------

    def get_case(self, case_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM cases WHERE case_key = ?", (case_key,)
        ).fetchone()

    def list_cases(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute("SELECT * FROM cases ORDER BY case_seq, contract_id")
        )

    def _next_case_seq(self) -> int:
        row = self.connection.execute("SELECT COALESCE(MAX(case_seq), 0) + 1 AS n FROM cases")
        return int(row.fetchone()["n"])

    def ensure_case(
        self,
        identity: dict[str, Any],
        contract_id: str,
        contract: dict[str, Any],
        detected_at: datetime,
        created_event_seq: int,
        primary_deadline_minutes: int,
    ) -> tuple[sqlite3.Row, bool]:
        """按 run_key+finding_digest+contract 幂等取得/创建案件。

        身份任一元素变化（finding 消失后重开、摘要或 run_key 变化）都会得到
        新 case_key；旧案保留且其 ACK 无法归属新案。
        """
        key = case_identity(identity["run_key"], identity["finding_digest"], contract_id)
        existing = self.get_case(key)
        if existing is not None:
            return existing, False
        if contract_id == "rapid_containment":
            deadline = detected_at + timedelta(minutes=contract["primary_deadline_minutes"])
            window_close = None
        else:
            # 响应截止与 rapid 合同同为 detected_at + primary_deadline_minutes
            # （09:05）；有界迟到窗口在截止之上再加 lateness_minutes（→09:12）。
            deadline = detected_at + timedelta(
                minutes=primary_deadline_minutes
            )
            window_close = deadline + timedelta(minutes=contract["lateness_minutes"])
        row = self.connection.execute(
            """INSERT INTO cases(case_key, case_seq, run_key, finding_digest, contract_id,
                                 snapshot_id, finding_code, finding_file, finding_row,
                                 contract_json, detected_at, deadline_at, window_close_at,
                                 status, report_state, created_event_seq, superseded)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (
                key,
                self._next_case_seq(),
                identity["run_key"],
                identity["finding_digest"],
                contract_id,
                identity["snapshot_id"],
                identity["finding_code"],
                identity["finding_file"],
                None if identity["finding_row"] is None else str(identity["finding_row"]),
                canonical_json(contract),
                fmt_ts(detected_at),
                None if deadline is None else fmt_ts(deadline),
                None if window_close is None else fmt_ts(window_close),
                "OPENED",
                "DETECTION",
                created_event_seq,
            ),
        )
        self._record_transition(key, created_event_seq, "OPENED", "OPENED",
                                "case provisioned from audit finding identity",
                                fmt_ts(detected_at))
        return self.get_case(key), True

    def mark_superseded(self, run_key: str, finding_digest: str) -> None:
        """同一 finding 身份换新后，阻断旧 ACK 的跨案归属。"""
        self.connection.execute(
            """UPDATE cases SET superseded = 1,
                 report_state = CASE WHEN report_state = 'DETECTION'
                                     THEN 'SUPERSEDED' ELSE report_state END
               WHERE run_key = ? AND finding_digest = ? AND superseded = 0""",
            (run_key, finding_digest),
        )

    # -- 事件落账 ----------------------------------------------------------

    def journal_event(self, event: dict[str, Any], status: str, detail: str) -> int:
        cur = self.connection.execute(
            """INSERT INTO event_journal(event_id, kind, occurred_at, received_at,
                                         payload_hash, event_run_key, intake_status, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["event_id"],
                event["kind"],
                event["occurred_at"],
                event["received_at"],
                event["_payload_hash"],
                event.get("run_key"),
                status,
                detail,
            ),
        )
        return int(cur.lastrowid)

    def find_event(self, event_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM event_journal WHERE event_id = ? ORDER BY event_seq",
            (event_id,),
        ).fetchone()

    def list_journal(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute("SELECT * FROM event_journal ORDER BY event_seq")
        )

    def note_case_event(self, case_key: str, event_seq: int, effect: str,
                        detail: str = "") -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO case_events(case_key, event_seq, effect, detail)
               VALUES (?, ?, ?, ?)""",
            (case_key, event_seq, effect, detail),
        )

    # -- 状态机 ------------------------------------------------------------

    def current_status(self, case_key: str) -> str:
        return self.get_case(case_key)["status"]

    def transition(self, case_key: str, event_seq: int, to_status: str,
                   note: str, at_time: datetime) -> str:
        """同一 (案件, 事件, 目标状态) 由主键去重，重放不产生重复转移。

        仅当确实写入一条新转移时才推进 cases.status；INSERT OR IGNORE 命中
        已有行（重放）时保持现状，避免任何旁路重复驱动状态机。
        """
        current = self.current_status(case_key)
        cur = self.connection.execute(
            """INSERT OR IGNORE INTO transitions(case_key, event_seq, from_status,
                                                 to_status, note, at_time)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (case_key, event_seq, current, to_status, note, fmt_ts(at_time)),
        )
        if cur.rowcount > 0 and current != to_status:
            self.connection.execute(
                "UPDATE cases SET status = ? WHERE case_key = ?", (to_status, case_key)
            )
        return to_status

    def _record_transition(self, case_key: str, event_seq: int, from_status: str,
                           to_status: str, note: str, at_time: str) -> None:
        self.connection.execute(
            """INSERT OR IGNORE INTO transitions(case_key, event_seq, from_status,
                                                 to_status, note, at_time)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (case_key, event_seq, from_status, to_status, note, at_time),
        )

    def set_report_state(self, case_key: str, state: str) -> None:
        self.connection.execute(
            "UPDATE cases SET report_state = ? WHERE case_key = ? AND report_state != ?",
            (state, case_key, state),
        )

    def add_signoff(self, case_key: str, role: str, event_seq: int,
                    occurred_at: str, received_at: str) -> bool:
        cur = self.connection.execute(
            """INSERT OR IGNORE INTO case_signoffs(case_key, role, event_seq,
                                                    occurred_at, received_at)
               VALUES (?, ?, ?, ?, ?)""",
            (case_key, role, event_seq, occurred_at, received_at),
        )
        return cur.rowcount > 0

    def signoffs(self, case_key: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM case_signoffs WHERE case_key = ? ORDER BY role", (case_key,)
            )
        )

    def has_signoff(self, case_key: str, role: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM case_signoffs WHERE case_key = ? AND role = ?",
            (case_key, role),
        ).fetchone() is not None

    # -- outbox ------------------------------------------------------------

    def add_intent(
        self,
        case_key: str,
        event_seq: int,
        kind: str,
        recipient: str | None,
        created_at: datetime,
        extra_payload: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """以稳定 intent_id 幂等写入本地意图；重放返回 (id, False)。"""
        intent_id = sha256_text(
            canonical_json(
                {
                    "case_key": case_key,
                    "event_seq": event_seq,
                    "intent_kind": kind,
                    "recipient": recipient,
                }
            )
        )
        payload = {
            "intent_kind": kind,
            "case_key": case_key,
            "event_seq": event_seq,
            "recipient": recipient,
        }
        if extra_payload:
            payload.update(extra_payload)
        cur = self.connection.execute(
            """INSERT OR IGNORE INTO outbox(intent_id, case_key, event_seq, intent_kind,
                                            recipient, payload_json, created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')""",
            (
                intent_id,
                case_key,
                event_seq,
                kind,
                recipient,
                canonical_json(payload),
                fmt_ts(created_at),
            ),
        )
        return intent_id, cur.rowcount > 0

    def list_outbox(self, case_key: str | None = None) -> list[sqlite3.Row]:
        if case_key is None:
            return list(self.connection.execute(
                "SELECT * FROM outbox ORDER BY event_seq, intent_id"))
        return list(self.connection.execute(
            "SELECT * FROM outbox WHERE case_key = ? ORDER BY event_seq, intent_id",
            (case_key,),
        ))

    def pending_intents(self) -> list[sqlite3.Row]:
        """外部投递层只拿得到带稳定 intent_id 的待确认意图。"""
        return list(self.connection.execute(
            "SELECT * FROM outbox WHERE status = 'PENDING' ORDER BY event_seq, intent_id"
        ))

    def confirm_delivery(self, intent_id: str, confirmed_at: datetime,
                         token: dict[str, Any] | None = None) -> bool:
        """幂等确认：重复确认同一 intent_id 不报错、不产生新效果。

        普通网络发送不天然 exactly-once；投递层必须凭稳定 intent_id 做
        去重确认，本方法只记录确认事实，不假装发送本身恰好一次。
        """
        stamp = fmt_ts(confirmed_at)
        with self.transaction():
            # 存在性检查与状态更新放在同一事务内，消除检查/更新间的 TOCTOU。
            existing = self.connection.execute(
                "SELECT status FROM outbox WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if existing is None:
                raise AuditError("INCIDENT_INTENT_UNKNOWN", f"未知 intent_id: {intent_id}")
            if existing["status"] == "CONFIRMED":
                return False
            self.connection.execute(
                "UPDATE outbox SET status = 'CONFIRMED', confirmed_at = ? WHERE intent_id = ?",
                (stamp, intent_id),
            )
            self.connection.execute(
                """INSERT OR IGNORE INTO delivery_confirmations(intent_id, confirmed_at,
                                                                token_json)
                   VALUES (?, ?, ?)""",
                (intent_id, stamp, canonical_json(token or {})),
            )
        return True

    def get_case_for_intent(self, intent_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT c.* FROM cases c JOIN outbox o ON o.case_key = c.case_key
               WHERE o.intent_id = ?""",
            (intent_id,),
        ).fetchone()

    def transitions(self, case_key: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM transitions WHERE case_key = ? ORDER BY event_seq, to_status",
            (case_key,),
        ))

    def case_events(self, case_key: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM case_events WHERE case_key = ? ORDER BY event_seq",
            (case_key,),
        ))


# ---------------------------------------------------------------------------
# 引擎：审计绑定、事件摄入、双合约状态机
# ---------------------------------------------------------------------------

def bind_finding(data: dict[str, Any], result) -> dict[str, Any]:
    """复核审计 finding 仍存在且摘要未变，返回稳定身份材料。"""
    if result.run_key != data["run_key"]:
        raise AuditError("INCIDENT_IDENTITY_CHANGED",
                         "run_key 变化：必须开新 case，旧 ACK 不得跨案归属")
    if result.workspace.snapshot.snapshot_id != data["snapshot_id"]:
        raise AuditError("INCIDENT_IDENTITY_CHANGED", "snapshot_id 与事故绑定不一致")
    matches = [item for item in result.findings
               if item.code == data["finding_code"] and item.file == data["finding_file"]
               and item.row == data["finding_row"]]
    if len(matches) != 1:
        raise AuditError("INCIDENT_FINDING_GONE",
                         "审计 finding 消失或不再唯一：旧案 ACK 阻断，需开新 case")
    digest = sha256_text(canonical_json(matches[0].to_dict()))
    if digest != data["finding_digest"]:
        raise AuditError("INCIDENT_IDENTITY_CHANGED",
                         "finding 摘要变化：必须开新 case，旧 ACK 不得跨案归属")
    return {
        "run_key": result.run_key,
        "finding_digest": digest,
        "snapshot_id": result.workspace.snapshot.snapshot_id,
        "finding_code": matches[0].code,
        "finding_file": matches[0].file,
        "finding_row": matches[0].row,
    }


def _apply_rapid(ledger: IncidentLedger, case: sqlite3.Row, event: dict[str, Any],
                 event_seq: int, raw: dict[str, Any]) -> None:
    """rapid_containment：received_at 钟，五分钟截止，迟到 ACK 不撤备援页。"""
    key = case["case_key"]
    deadline = parse_ts(case["deadline_at"], "case.deadline_at")
    received = event["_received"]
    if event["kind"] == "TIMER":
        if received < deadline:
            ledger.note_case_event(key, event_seq, "TIMER_EARLY",
                                   "timer fired before primary deadline; ignored")
            return
        if ledger.has_signoff(key, "primary"):
            ledger.note_case_event(key, event_seq, "TIMER_ACKED",
                                   "primary already acked by received clock; no backup page")
            return
        ledger.transition(key, event_seq, "BACKUP_PAGED",
                          "deadline timer with no received primary ACK", received)
        ledger.add_intent(key, event_seq, "PAGE_BACKUP", raw["backup"], received,
                          {"clock": "received_at", "deadline_at": case["deadline_at"]})
        ledger.set_report_state(key, "EXPORT_PAUSED_PENDING")
        ledger.note_case_event(key, event_seq, "PAGE_BACKUP",
                               "one-shot backup page at received-clock deadline")
        return
    if event["kind"] == "PRIMARY_ACK":
        if ledger.add_signoff(key, "primary", event_seq,
                              event["occurred_at"], event["received_at"]):
            ledger.note_case_event(key, event_seq, "PRIMARY_ACK_RECORDED",
                                   "late primary ACK retained but cannot revoke backup page")
        # 注意：case 是开立时抓取的 sqlite3.Row 快照，状态会随后续转移变化，
        # 必须实时查询，不能读 case["status"]（否则同刻 TIMER 先备援、ACK 后到
        # 时会把案件错误地改回 PRIMARY_ACKED，抹掉已发备援页）。
        if received <= deadline and ledger.current_status(key) == "OPENED":
            ledger.transition(key, event_seq, "PRIMARY_ACKED",
                              "primary ACK received within deadline", received)
            ledger.set_report_state(key, "EXPORT_PAUSED_ACKED")
        else:
            # 迟到 ACK：合同承诺 KEEP_BACKUP_PAGE，报告出口保持暂停。
            ledger.transition(key, event_seq, "PRIMARY_ACK_LATE",
                              "primary ACK received after deadline; backup page kept",
                              received)
            ledger.set_report_state(key, "EXPORT_PAUSED_PENDING")
        return
    if event["kind"] == "STEWARD_ACK":
        ledger.note_case_event(key, event_seq, "STEWARD_ACK_IGNORED",
                               "rapid_containment does not consume steward signoff")


def _maybe_complete_evidence_case(ledger: IncidentLedger, case: sqlite3.Row,
                                  event_seq: int, received: datetime) -> None:
    """双签完成裁定：无论哪个 ACK 先到，签收齐备即产出一次 CASE_FILE_READY。"""
    key = case["case_key"]
    if ledger.current_status(key) == "CASE_FILE_READY":
        return
    roles = {item["role"] for item in ledger.signoffs(key)}
    if {"primary", "steward"} <= roles:
        ledger.transition(key, event_seq, "CASE_FILE_READY",
                          "both required signoffs present; read-only case file complete",
                          received)
        ledger.add_intent(key, event_seq, "CASE_FILE_READY", None, received,
                          {"read_only": True, "writeback": "none"})
        ledger.set_report_state(key, "CASE_FILE_READY")


def _apply_evidence(ledger: IncidentLedger, case: sqlite3.Row, event: dict[str, Any],
                    event_seq: int, raw: dict[str, Any]) -> None:
    """evidence_review：occurred_at 钟 + 有界迟到窗口，双签成案，不写回审计数据。"""
    key = case["case_key"]
    deadline = parse_ts(case["deadline_at"], "case.deadline_at")
    window_close = parse_ts(case["window_close_at"], "case.window_close_at")
    occurred = event["_occurred"]
    received = event["_received"]
    if event["kind"] == "TIMER":
        ledger.note_case_event(key, event_seq, "TIMER_IGNORED",
                               "evidence_review adjudicates on bounded-lateness window")
        return
    if received > window_close:
        ledger.note_case_event(
            key, event_seq, "ACK_OUTSIDE_WINDOW",
            f"received {event['received_at']} after window close {case['window_close_at']}",
        )
        return
    if event["kind"] == "PRIMARY_ACK":
        if not ledger.add_signoff(key, "primary", event_seq,
                                  event["occurred_at"], event["received_at"]):
            return
        if occurred <= deadline:
            ledger.transition(key, event_seq, "PRIMARY_ACKED_TIMELY",
                              "occurred before deadline and received inside lateness window",
                              received)
            ledger.note_case_event(key, event_seq, "NO_BACKUP_PAGE",
                                   "timely ACK within bounded lateness window")
            ledger.set_report_state(key, "CASE_PINNED_ACKED")
        else:
            ledger.transition(key, event_seq, "PRIMARY_ACK_LATE",
                              "ACK occurred after deadline even though received in window",
                              received)
            ledger.add_intent(key, event_seq, "PAGE_BACKUP", raw["backup"], received,
                              {"clock": "occurred_at_with_bounded_lateness",
                               "deadline_at": case["deadline_at"],
                               "window_close_at": case["window_close_at"]})
            ledger.set_report_state(key, "CASE_PINNED_BACKUP")
        # 管理员 ACK 可能先到：主签落账后立即复核双签完成。
        _maybe_complete_evidence_case(ledger, case, event_seq, received)
        return
    if event["kind"] == "STEWARD_ACK":
        if not ledger.add_signoff(key, "steward", event_seq,
                                  event["occurred_at"], event["received_at"]):
            return
        ledger.note_case_event(key, event_seq, "STEWARD_ACK_RECORDED",
                               "evidence steward signoff recorded; no audit data writeback")
        _maybe_complete_evidence_case(ledger, case, event_seq, received)


def _drain_evidence_windows(ledger: IncidentLedger, as_of: datetime) -> None:
    """窗口已关闭仍无主签的 evidence 案件，补发一次备援页。"""
    for case in ledger.list_cases():
        if case["contract_id"] != "evidence_review" or case["superseded"]:
            continue
        window_close = parse_ts(case["window_close_at"], "case.window_close_at")
        if as_of < window_close or ledger.has_signoff(case["case_key"], "primary"):
            continue
        if any(row["intent_kind"] == "PAGE_BACKUP" for row in ledger.list_outbox(case["case_key"])):
            continue
        # 以窗口关闭时刻为合成裁定事件（seq=0 表示系统裁定而非外部事件）。
        ledger.transition(case["case_key"], 0, "BACKUP_PAGED",
                          "lateness window closed without any in-window primary ACK",
                          window_close)
        ledger.add_intent(case["case_key"], 0, "PAGE_BACKUP", None, window_close,
                          {"clock": "occurred_at_with_bounded_lateness",
                           "window_close_at": case["window_close_at"],
                           "adjudication": "window_expiry"})
        ledger.set_report_state(case["case_key"], "CASE_PINNED_BACKUP")


def ingest_fixture(
    ledger: IncidentLedger,
    data: dict[str, Any],
    result,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """把一次 fixture 摄入台账：绑定 finding、落事件、跑两个合约状态机。

    全程按 received_at 排序进入台账（同刻按输入顺序），事件落账、状态转移
    与 outbox 意图在同一事务内提交。返回结构化运行摘要。
    """
    validated = validate_fixture(data)
    raw = validated["raw"]
    identity = bind_finding(raw, result)
    detected_at = parse_ts(data["detected_at"], "detected_at")
    primary_deadline_minutes = data.get("primary_deadline_minutes")
    if not isinstance(primary_deadline_minutes, int) or primary_deadline_minutes <= 0:
        raise AuditError("INCIDENT_INPUT_INVALID",
                         "primary_deadline_minutes 必须是正整数")
    contracts = validated["contracts"]

    # 按 received_at 进入台账；同刻保留 fixture 的输入顺序（不按 occurred_at
    # 全局重排，否则会抹掉 09:05 已实际发生的超时）。
    input_index = {id(item): index for index, item in enumerate(validated["events"])}
    ordered = sorted(
        validated["events"],
        key=lambda item: (item["_received"], input_index[id(item)]),
    )

    rejections: list[dict[str, str]] = []
    duplicates: list[dict[str, str]] = []
    with ledger.transaction():
        # 同一 finding 身份若曾以旧 run_key/摘要建过案，先阻断旧案跨案归属。
        prior = ledger.connection.execute(
            "SELECT DISTINCT run_key, finding_digest FROM cases "
            "WHERE finding_code = ? AND finding_file = ? AND COALESCE(finding_row, '') = ?",
            (identity["finding_code"], identity["finding_file"],
             "" if identity["finding_row"] is None else str(identity["finding_row"])),
        ).fetchall()
        for row in prior:
            if (row["run_key"], row["finding_digest"]) != (
                identity["run_key"], identity["finding_digest"]
            ):
                ledger.mark_superseded(row["run_key"], row["finding_digest"])

        cases: dict[str, sqlite3.Row] = {}
        for contract_id, contract in contracts.items():
            case, created = ledger.ensure_case(
                identity, contract_id, contract, detected_at, 0,
                primary_deadline_minutes,
            )
            cases[contract_id] = case
            if created:
                if contract_id == "rapid_containment":
                    ledger.add_intent(case["case_key"], 0, "PAGE_PRIMARY",
                                      raw["primary"], detected_at,
                                      {"stage": "on_detection"})
                    ledger.add_intent(case["case_key"], 0, "PAUSE_CURRENT_EXPORT",
                                      None, detected_at, {"stage": "on_detection"})
                else:
                    ledger.add_intent(case["case_key"], 0, "PAGE_PRIMARY",
                                      raw["primary"], detected_at,
                                      {"stage": "on_detection"})
                    ledger.add_intent(case["case_key"], 0, "PIN_READ_ONLY_CASE",
                                      None, detected_at,
                                      {"stage": "on_detection", "read_only": True,
                                       "writeback": "none"})

        for event in ordered:
            existing = ledger.find_event(event["event_id"])
            if existing is not None:
                if existing["payload_hash"] != event["_payload_hash"]:
                    # 同 ID 不同 payload：硬拒绝，不允许复用事件身份。
                    raise AuditError(
                        "INCIDENT_EVENT_ID_CONFLICT",
                        f"event_id {event['event_id']} 已以不同 payload 落账，拒绝复用",
                    )
                seq = existing["event_seq"]
                # 同一业务事件的重复投递：每次额外到达都记入 redelivery 明细，
                # 但不再次驱动状态机，也不生成新 outbox 意图。
                ledger.connection.execute(
                    """INSERT OR IGNORE INTO event_redeliveries(event_seq, received_at)
                       VALUES (?, ?)""",
                    (seq, event["received_at"]),
                )
                redeliveries = [
                    row["received_at"] for row in ledger.connection.execute(
                        "SELECT received_at FROM event_redeliveries WHERE event_seq = ? "
                        "ORDER BY received_at", (seq,))
                ]
                duplicates.append({"event_id": event["event_id"], "event_seq": seq,
                                   "first_received_at": existing["received_at"],
                                   "redelivered_at": redeliveries})
                continue
            # 旧 run_key 的 ACK：隔离拒绝，绝不关闭当前审计事故。
            # 显式携带 run_key（含空串）但与当前事故不一致即隔离；不带该
            # 字段的 ACK 视为归属当前审计 run。
            if event["kind"] in ("PRIMARY_ACK", "STEWARD_ACK") and event.get("run_key") is not None:
                if event["run_key"] != identity["run_key"]:
                    seq = ledger.journal_event(
                        event, "REJECTED_STALE_RUN_KEY",
                        f"ACK 绑定 run_key {event['run_key']}，当前事故 {identity['run_key']}",
                    )
                    rejections.append(
                        {"event_id": event["event_id"], "event_seq": seq,
                         "reason": "STALE_RUN_KEY"}
                    )
                    continue
            seq = ledger.journal_event(event, "ACCEPTED", "")
            for contract_id, case in cases.items():
                # 实时读取 superseded：同一摄入内旧身份案件可能刚被标记。
                if ledger.get_case(case["case_key"])["superseded"]:
                    continue
                if contract_id == "rapid_containment":
                    _apply_rapid(ledger, case, event, seq, raw)
                else:
                    _apply_evidence(ledger, case, event, seq, raw)
                # 具体 effect 已存在时不覆盖；只给未单独记账的事件补 CONSUMED。
                if ledger.connection.execute(
                    "SELECT 1 FROM case_events WHERE case_key = ? AND event_seq = ?",
                    (case["case_key"], seq),
                ).fetchone() is None:
                    ledger.note_case_event(case["case_key"], seq, "CONSUMED",
                                           event["kind"])

        if as_of is None:
            as_of = max(item["_received"] for item in validated["events"])
        if isinstance(as_of, str):
            as_of = parse_ts(as_of, "as_of")
        _drain_evidence_windows(ledger, as_of)

    return {
        "identity": identity,
        "cases": cases,
        "rejections": rejections,
        "duplicates": duplicates,
        "as_of": fmt_ts(as_of),
    }


# ---------------------------------------------------------------------------
# 只读报告
# ---------------------------------------------------------------------------

def _case_report(ledger: IncidentLedger, case: sqlite3.Row) -> dict[str, Any]:
    key = case["case_key"]
    journal = {row["event_seq"]: row for row in ledger.list_journal()}
    trajectory = []
    for row in ledger.transitions(key):
        event_seq = row["event_seq"]
        if event_seq == 0:
            event_id = "system"
            occurred_at = received_at = row["at_time"]
        else:
            event = journal[event_seq]
            occurred_at = event["occurred_at"]
            received_at = event["received_at"]
            event_id = event["event_id"]
        trajectory.append({
            "event_id": event_id,
            "event_seq": event_seq,
            "occurred_at": occurred_at,
            "received_at": received_at,
            "adjudicated_at": row["at_time"],
            "from_status": row["from_status"],
            "to_status": row["to_status"],
            "note": row["note"],
        })
    intents = [
        {
            "intent_id": row["intent_id"],
            "intent_kind": row["intent_kind"],
            "recipient": row["recipient"],
            "event_seq": row["event_seq"],
            "status": row["status"],
            "created_at": row["created_at"],
            "confirmed_at": row["confirmed_at"],
        }
        for row in ledger.list_outbox(key)
    ]
    signoffs = [
        {"role": row["role"], "event_seq": row["event_seq"],
         "occurred_at": row["occurred_at"], "received_at": row["received_at"]}
        for row in ledger.signoffs(key)
    ]
    human_signoff = {
        "primary_acked": ledger.has_signoff(key, "primary"),
        "steward_acked": ledger.has_signoff(key, "steward"),
        "signoffs": signoffs,
    }
    return {
        "incident_key": key,
        "contract_id": case["contract_id"],
        "run_key": case["run_key"],
        "finding_digest": case["finding_digest"],
        "identity_binding": {
            "run_key": case["run_key"],
            "finding_digest": case["finding_digest"],
            "contract": case["contract_id"],
        },
        "detected_at": case["detected_at"],
        "deadline_at": case["deadline_at"],
        "window_close_at": case["window_close_at"],
        "status": case["status"],
        "report_state": case["report_state"],
        "superseded": bool(case["superseded"]),
        "state_trajectory": trajectory,
        "human_signoff": human_signoff,
        "outbox": intents,
    }


def build_report(ledger: IncidentLedger) -> dict[str, Any]:
    journal_rows = [row for row in ledger.list_journal() if row["event_seq"] != 0]
    intake = []
    for row in journal_rows:
        redeliveries = [
            item["received_at"] for item in ledger.connection.execute(
                "SELECT received_at FROM event_redeliveries WHERE event_seq = ? "
                "ORDER BY received_at", (row["event_seq"],))
        ]
        intake.append({
            "event_seq": row["event_seq"],
            "event_id": row["event_id"],
            "kind": row["kind"],
            "occurred_at": row["occurred_at"],
            "received_at": row["received_at"],
            "redelivered_at": redeliveries,
            "intake_status": row["intake_status"],
            "detail": row["detail"],
        })
    cases = [_case_report(ledger, case) for case in ledger.list_cases()]
    outbox_rows = ledger.list_outbox()
    # 重放摘要只纳入稳定身份字段（intent_id/案件/事件/意图种类），不含
    # PENDING/CONFIRMED 等投递状态——确认动作不应改变重放指纹。
    replay_summary = sha256_text(
        canonical_json(
            [
                {
                    "intent_id": row["intent_id"],
                    "intent_kind": row["intent_kind"],
                    "case_key": row["case_key"],
                    "event_seq": row["event_seq"],
                }
                for row in outbox_rows
            ]
        )
    )
    return {
        "mode": "OFFLINE_LOCAL_INTENTS_ONLY",
        "notice": "合成离线事件：仅生成本地意图/台账，不连接任何实际寻呼或通知服务",
        "journal_order": "received_at（同刻按输入顺序）；occurred_at 原样保留不全局重排",
        "intake_journal": intake,
        "contracts": cases,
        "outbox_summary": {
            "total": len(outbox_rows),
            "pending": sum(1 for row in outbox_rows if row["status"] == "PENDING"),
            "confirmed": sum(1 for row in outbox_rows if row["status"] == "CONFIRMED"),
            "replay_digest": replay_summary,
        },
    }


# ---------------------------------------------------------------------------
# 崩溃点模拟与恢复重放
# ---------------------------------------------------------------------------

def confirm_intent(ledger: IncidentLedger, intent_id: str,
                   confirmed_at: datetime,
                   token: dict[str, Any] | None = None) -> bool:
    """外部投递层的幂等确认入口（模块级便捷包装）。"""
    return ledger.confirm_delivery(intent_id, confirmed_at, token)


class CrashAfterCommitError(RuntimeError):
    """模拟“事件已落账/意图已提交，但投递确认前进程崩溃”。"""


def run_with_crashpoint(
    db_path: str | Path,
    fixture_path: str | Path,
    *,
    lake_root: str | Path | None = None,
    crash_after_events: bool = False,
    as_of: datetime | str | None = None,
) -> dict[str, Any]:
    """带崩溃点的运行入口。

    crash_after_events=True 时，事务提交后立刻抛出 CrashAfterCommitError：
    磁盘上的台账已含全部状态转移与 outbox 意图，只是没有任何投递确认。
    重新打开同一 db_path 重放即可证明意图不重复生成。
    """
    fixture_path = Path(fixture_path)
    root = Path(lake_root) if lake_root is not None else Path(
        __file__).resolve().parents[2] / "examples" / "incident" / "lake"
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    result = execute_audit(root)
    with IncidentLedger(db_path) as ledger:
        ingest_fixture(ledger, data, result, as_of=as_of)
        if crash_after_events:
            raise CrashAfterCommitError(
                "simulated crash after journal commit, before delivery confirmation"
            )
        return build_report(ledger)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="离线事故台账与本地 outbox（不连接真实通知服务）")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=None,
                        help="SQLite 台账路径，默认 .incident/handoff.sqlite")
    parser.add_argument("--lake", type=Path, default=None,
                        help="审计 lake 根目录，默认 examples/incident/lake")
    parser.add_argument("--as-of", dest="as_of", default=None,
                        help="窗口裁定时刻，默认取事件最大 received_at")
    parser.add_argument("--crash-after-events", action="store_true",
                        help="事件落账后、投递确认前模拟崩溃")
    args = parser.parse_args(argv)

    data = json.loads(args.fixture.read_text(encoding="utf-8"))
    root = args.lake or Path(__file__).resolve().parents[2] / "examples" / "incident" / "lake"
    result = execute_audit(root)
    db_path = args.db or Path(".incident") / "handoff.sqlite"
    with IncidentLedger(db_path) as ledger:
        ingest_fixture(ledger, data, result, as_of=args.as_of)
        if args.crash_after_events:
            # 事务已提交；进程在此退出，外部投递层尚未确认任何意图。
            raise CrashAfterCommitError(
                "simulated crash after journal commit, before delivery confirmation"
            )
        report = build_report(ledger)
    report["database"] = str(db_path)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
