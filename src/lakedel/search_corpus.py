"""Build a read-only document corpus from audited visible lake rows."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .engine import execute_audit


def inspect_search_corpus(workspace: Path, catalog_path: Path) -> dict[str, Any]:
    """Report the live corpus and term statistics without choosing results."""
    audit = execute_audit(workspace)
    if audit.stats["diffs"]:
        raise ValueError("search corpus differs from expected visibility")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    if catalog.get("snapshot_id") != audit.workspace.snapshot.snapshot_id:
        raise ValueError("catalog snapshot differs from audited snapshot")
    documents = catalog.get("documents")
    if not isinstance(documents, list):
        raise ValueError("catalog documents must be a list")
    rows = {row.row_id: row for row in audit.workspace.rows}
    if len(rows) != len(audit.workspace.rows):
        raise ValueError("audited row identities are not unique")
    by_id: dict[str, dict[str, Any]] = {}
    for item in documents:
        if not isinstance(item, dict) or not isinstance(item.get("row_id"), str):
            raise ValueError("invalid search document")
        row_id = item["row_id"]
        if row_id in by_id or row_id not in rows:
            raise ValueError("duplicate or unknown search row")
        if item.get("region") != rows[row_id].values.get("region"):
            raise ValueError("catalog region does not match audited row")
        terms = item.get("terms")
        if (not isinstance(terms, list) or not terms
                or any(not isinstance(term, str) or not term.islower() or not term.isalpha()
                       for term in terms)):
            raise ValueError("search terms must be lowercase alphabetic tokens")
        by_id[row_id] = item
    if set(by_id) != set(rows):
        raise ValueError("catalog must cover every audited row")

    visible = [row.row_id for row, decision in zip(audit.workspace.rows, audit.decisions)
               if decision.visible]
    document_frequency: Counter[str] = Counter()
    for row_id in visible:
        document_frequency.update(set(by_id[row_id]["terms"]))
    return {
        "run_key": audit.run_key,
        "snapshot_id": audit.workspace.snapshot.snapshot_id,
        "visible_document_ids": sorted(visible),
        "visible_count": len(visible),
        "visibility_findings": audit.stats["findings"],
        "term_document_frequency": dict(sorted(document_frequency.items())),
        "top_k": None,
    }
