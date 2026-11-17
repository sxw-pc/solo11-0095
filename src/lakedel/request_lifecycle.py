"""Read-only fulfillment history for declared row-erasure requests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import execute_audit
from .errors import AuditError
from .util import load_json_file, safe_join
from .workspace import load_workspace

_UNTRUSTED = {
    "FILE_HASH_MISMATCH", "FILE_DECODE_ERROR", "SCHEMA_HASH_MISMATCH",
    "DUPLICATE_ID", "ORPHAN_DELETE_TARGET", "DELETE_SCHEMA_MISMATCH",
    "INVALID_JSON_LINE", "DELETE_ENTRY_INVALID", "MANIFEST_TYPE_ERROR",
}


def _instant(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AuditError("REQUEST_INVALID", f"{label} must be a UTC timestamp ending in Z")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuditError("REQUEST_INVALID", f"{label} is not a timestamp") from exc
    if result.tzinfo != timezone.utc:
        raise AuditError("REQUEST_INVALID", f"{label} must be UTC")
    return result


def _requests(root: Path, relative_path: str) -> list[dict[str, Any]]:
    raw = load_json_file(safe_join(root, relative_path))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise AuditError("REQUEST_INVALID", "request document must have version 1")
    items = raw.get("requests")
    if not isinstance(items, list) or not items or len(items) > 128:
        raise AuditError("REQUEST_INVALID", "requests must contain 1..128 entries")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise AuditError("REQUEST_INVALID", f"requests[{index}] must be an object")
        rid = item.get("request_id")
        row_ids = item.get("row_ids")
        if not isinstance(rid, str) or not rid or rid in seen:
            raise AuditError("REQUEST_INVALID", "request_id must be unique and nonempty")
        if (not isinstance(row_ids, list) or not row_ids or len(row_ids) > 128
                or any(not isinstance(v, str) or not v for v in row_ids)
                or len(row_ids) != len(set(row_ids))):
            raise AuditError("REQUEST_INVALID", f"{rid}: row_ids must be unique strings")
        accepted = _instant(item.get("accepted_at"), f"{rid}.accepted_at")
        due = _instant(item.get("due_at"), f"{rid}.due_at")
        if due < accepted:
            raise AuditError("REQUEST_INVALID", f"{rid}: due_at precedes accepted_at")
        seen.add(rid)
        result.append({"request_id": rid, "row_ids": tuple(row_ids),
                       "accepted_at": accepted, "due_at": due})
    return sorted(result, key=lambda item: item["request_id"])


def check_requests(
    workspace: str | Path, request_file: str = "requests/erasure_requests.json",
    through_snapshot: str | None = None,
) -> dict[str, Any]:
    """Evaluate request state at each named local snapshot, without writing data."""
    root = Path(workspace).resolve()
    first = load_workspace(root)
    requests = _requests(root, request_file)
    ordered = sorted(first.snapshots, key=lambda s: (s.sequence_number, s.snapshot_id))
    if not ordered or len(ordered) > 64:
        raise AuditError("REQUEST_INVALID", "snapshot count must be 1..64")
    ids = [s.snapshot_id for s in ordered]
    if through_snapshot is not None:
        if through_snapshot not in ids:
            raise AuditError("SNAPSHOT_NOT_FOUND", "through snapshot does not exist")
        ordered = ordered[:ids.index(through_snapshot) + 1]
    previous_sequence = -1
    previous_time: datetime | None = None
    audits = []
    for snapshot in ordered:
        instant = _instant(snapshot.created_at, f"{snapshot.snapshot_id}.created_at")
        if snapshot.sequence_number <= previous_sequence or (
            previous_time is not None and instant <= previous_time
        ):
            raise AuditError("REQUEST_INVALID", "snapshot sequence and time must increase")
        previous_sequence, previous_time = snapshot.sequence_number, instant
        audits.append((snapshot, instant, execute_audit(root, snapshot.snapshot_id)))
    output = []
    for request in requests:
        target = set(request["row_ids"])
        seen_rows: set[str] = set()
        state = "pending"
        ever_fulfilled = False
        ever_regressed = False
        timeline = []
        for snapshot, instant, audit in audits:
            if instant < request["accepted_at"]:
                continue
            matches = [d for d in audit.decisions if d.row_id in target]
            seen_rows.update(d.row_id for d in matches if d.row_id is not None)
            visible = sorted({d.row_id for d in matches if d.visible and d.row_id is not None})
            evidence = [
                {"row_id": d.row_id, "file_id": d.file_id, "row_pos": d.row_pos,
                 "visible": d.visible, "reason_code": d.reason_code}
                for d in sorted(matches, key=lambda d: (d.row_id or "", d.file_id, d.row_pos))
            ]
            untrusted = sorted({f.code for f in audit.findings if f.code in _UNTRUSTED})
            if untrusted or target - seen_rows:
                state = "indeterminate"
            elif visible:
                if ever_fulfilled:
                    state = "regressed"
                    ever_regressed = True
                else:
                    state = "breached" if instant > request["due_at"] else "pending"
            elif ever_regressed:
                state = "regressed"
            else:
                state = "fulfilled_late" if instant > request["due_at"] else "fulfilled"
                ever_fulfilled = True
            timeline.append({"snapshot_id": snapshot.snapshot_id,
                             "observed_at": snapshot.created_at, "status": state,
                             "visible_row_ids": visible, "rows": evidence,
                             "untrusted_codes": untrusted})
        output.append({"request_id": request["request_id"],
                       "row_ids": list(request["row_ids"]),
                       "status": timeline[-1]["status"] if timeline else "not_started",
                       "timeline": timeline})
    return {"version": 1, "through_snapshot": ordered[-1].snapshot_id,
            "requests": output}
