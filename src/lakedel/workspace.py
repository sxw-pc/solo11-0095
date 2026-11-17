"""Workspace 发现、清单加载、引用文件校验与指纹。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import AuditError, Finding, finding
from .fileio import data_integrity, read_data_files
from .manifest import parse_snapshot, parse_table
from .models import Snapshot, WorkspaceData
from .reconcile import load_expected
from .util import (
    load_json_file,
    relative_posix,
    require_file,
    safe_join,
    sha256_file,
)


def _snapshot_files(root: Path) -> list[tuple[str, Path]]:
    directory = root / "snapshots"
    if not directory.is_dir():
        raise AuditError("SNAPSHOT_NOT_FOUND", "workspace 缺少 snapshots 目录", str(directory))
    result = []
    for path in sorted(directory.glob("*.json")):
        rel = relative_posix(path, root)
        result.append((rel, require_file(safe_join(root, rel), rel)))
    if not result:
        raise AuditError("SNAPSHOT_NOT_FOUND", "workspace 没有快照文件", str(directory))
    return result


def _select_snapshot(snapshots: list[Snapshot], snapshot_id: str | None) -> Snapshot:
    if snapshot_id is not None:
        selected = next(
            (item for item in snapshots if item.snapshot_id == snapshot_id), None
        )
        if selected is None:
            raise AuditError("SNAPSHOT_NOT_FOUND", f"未找到快照: {snapshot_id}")
        return selected
    return max(snapshots, key=lambda item: (item.sequence_number, item.snapshot_id))


def _fingerprint(
    root: Path,
    table_path: Path,
    snapshot: Snapshot,
    expected_path: Path,
) -> str:
    digest = hashlib.sha256()
    files: dict[str, Path] = {
        "table.json": table_path,
        snapshot.source: safe_join(root, snapshot.source),
        relative_posix(expected_path, root): expected_path,
    }
    for spec in snapshot.data_files:
        files[spec.path] = safe_join(root, spec.path)
    for spec in snapshot.delete_files:
        files[spec.path] = safe_join(root, spec.path)
    for rel in sorted(files):
        path = require_file(files[rel], rel)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_workspace(
    workspace: str | Path, snapshot_id: str | None = None
) -> WorkspaceData:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise AuditError("WORKSPACE_INVALID", f"workspace 不存在: {root}", str(root))
    findings: list[Finding] = []
    table_path = require_file(safe_join(root, "table.json"), "table.json")
    table = parse_table(load_json_file(table_path), "table.json")
    snapshots: list[Snapshot] = []
    seen_snapshots: set[str] = set()
    for source, path in _snapshot_files(root):
        snapshot = parse_snapshot(
            load_json_file(path), source, table.schema.schema_hash, findings
        )
        if snapshot.snapshot_id in seen_snapshots:
            findings.append(
                finding(
                    "DUPLICATE_ID", "重复快照 ID", source, None,
                    {"snapshot_id": snapshot.snapshot_id},
                )
            )
            continue
        seen_snapshots.add(snapshot.snapshot_id)
        snapshots.append(snapshot)
    if not snapshots:
        raise AuditError("SNAPSHOT_NOT_FOUND", "没有可用快照")
    selected = _select_snapshot(snapshots, snapshot_id)
    for spec in selected.data_files:
        require_file(safe_join(root, spec.path), spec.path)
        if spec.schema_hash != table.schema.schema_hash:
            findings.append(
                finding(
                    "SCHEMA_HASH_MISMATCH", "数据文件模式哈希与表模式不一致",
                    spec.path, None,
                    {"declared": spec.schema_hash, "expected": table.schema.schema_hash},
                )
            )
    for spec in selected.delete_files:
        require_file(safe_join(root, spec.path), spec.path)
    expected_path = require_file(
        safe_join(root, table.expected_visible), table.expected_visible
    )
    expected = load_expected(expected_path, findings)
    integrity = data_integrity(root, selected.data_files)
    rows, row_findings = read_data_files(
        root, selected.data_files, table.schema, integrity
    )
    findings.extend(row_findings)
    fingerprint = _fingerprint(root, table_path, selected, expected_path)
    return WorkspaceData(
        root,
        table,
        tuple(snapshots),
        selected,
        rows,
        expected,
        expected_path,
        fingerprint,
        findings,
        integrity,
    )
