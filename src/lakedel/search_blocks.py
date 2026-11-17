"""Validate an auditable block index and enforce its online query read budget."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine import execute_audit


class BlockQueryStore:
    """Only block summaries are free; opening a posting block spends the limit."""

    def __init__(self, workspace: Path, maximum_block_fetches: int):
        self.workspace = workspace
        self.maximum_block_fetches = maximum_block_fetches
        self.block_fetches = 0
        self.index = json.loads((workspace / "search_blocks.json").read_text(encoding="utf-8"))
        self._cache: dict[int, dict[str, list[dict[str, Any]]]] = {}

    def blocks(self, term: str) -> list[dict[str, Any]]:
        return self.index["postings"].get(term, [])

    def fetch_block(self, block_number: int) -> dict[str, list[dict[str, Any]]]:
        if block_number in self._cache:
            return self._cache[block_number]
        if self.block_fetches >= self.maximum_block_fetches:
            raise ValueError("query block fetch budget exceeded")
        content: dict[str, list[dict[str, Any]]] = {}
        for term in self.index["postings"]:
            metadata = next((item for item in self.blocks(term)
                             if item["block"] == block_number), None)
            if metadata is not None:
                content[term] = json.loads((self.workspace / metadata["path"]).read_text(encoding="utf-8"))
        if not content:
            raise ValueError("posting block is absent")
        self.block_fetches += 1
        self._cache[block_number] = content
        return content

    def fetch(self, term: str, block_number: int) -> list[dict[str, Any]]:
        if not any(item["block"] == block_number for item in self.blocks(term)):
            raise ValueError("term block is absent")
        return self.fetch_block(block_number)[term]


def inspect_search_blocks(workspace: Path) -> dict[str, Any]:
    """Build-time validation; future online ranking must use BlockQueryStore."""
    audit = execute_audit(workspace)
    if audit.stats["finding_errors"] or audit.stats["diffs"]:
        raise ValueError("search index requires a clean visibility audit")
    catalog = json.loads((workspace / "search_catalog.json").read_text(encoding="utf-8"))
    index = json.loads((workspace / "search_blocks.json").read_text(encoding="utf-8"))
    if (catalog.get("snapshot_id") != audit.workspace.snapshot.snapshot_id
            or index.get("snapshot_id") != audit.workspace.snapshot.snapshot_id
            or index.get("block_size") != 32):
        raise ValueError("search index does not match audited snapshot")
    documents = catalog.get("documents")
    if not isinstance(documents, list) or len(documents) != len(audit.workspace.rows):
        raise ValueError("catalog must cover audited row universe")
    positions = {item.get("row_id"): pos for pos, item in enumerate(documents)}
    if len(positions) != len(documents):
        raise ValueError("duplicate catalog row")
    rows = {row.row_id: row for row in audit.workspace.rows}
    if set(positions) != set(rows):
        raise ValueError("catalog row differs from audited row")
    for item in documents:
        if item.get("region") != rows[item["row_id"]].values.get("region"):
            raise ValueError("catalog region differs from audited row")
    visible = {row.row_id for row, decision in zip(audit.workspace.rows, audit.decisions)
               if decision.visible}
    from hashlib import sha256
    from .util import canonical_json

    visible_digest = sha256((canonical_json(sorted(visible)) + "\n").encode("utf-8")).hexdigest()
    if index.get("visible_rows_sha256") != visible_digest:
        raise ValueError("index visible-row digest differs from audit")
    postings = index.get("postings")
    if not isinstance(postings, dict):
        raise ValueError("search block directory is absent")
    term_df: dict[str, int] = {}
    seen_paths: set[str] = set()
    for term, blocks in postings.items():
        if not isinstance(term, str) or not isinstance(blocks, list):
            raise ValueError("invalid term directory")
        seen_rows: set[str] = set()
        seen_numbers: set[int] = set()
        for metadata in blocks:
            number, label = metadata.get("block"), metadata.get("path")
            if (not isinstance(number, int) or number in seen_numbers
                    or not isinstance(label, str) or label in seen_paths
                    or label != f"index/{term}/{number:02d}.json"):
                raise ValueError("invalid or repeated posting block")
            seen_numbers.add(number)
            seen_paths.add(label)
            items = json.loads((workspace / label).read_text(encoding="utf-8"))
            if not isinstance(items, list) or not items:
                raise ValueError("empty posting block")
            if metadata.get("max_tf") != max(item.get("tf", -1) for item in items):
                raise ValueError("block upper bound mismatch")
            region_max: dict[str, int] = {}
            for item in items:
                row_id, tf = item.get("row_id"), item.get("tf")
                if (row_id not in positions or row_id in seen_rows
                        or positions[row_id] // 32 != number
                        or isinstance(tf, bool) or not isinstance(tf, int) or tf <= 0):
                    raise ValueError("invalid posting")
                seen_rows.add(row_id)
                region = documents[positions[row_id]]["region"]
                region_max[region] = max(region_max.get(region, 0), tf)
            if metadata.get("max_tf_by_region") != region_max:
                raise ValueError("regional block upper bound mismatch")
        term_df[term] = len(seen_rows & visible)
    if index.get("visible_term_df") != term_df:
        raise ValueError("index active document frequency differs from audit")
    limit = catalog.get("query_budget", {}).get("maximum_block_fetches")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit < len(seen_paths):
        raise ValueError("query block budget is absent")
    return {
        "run_key": audit.run_key, "snapshot_id": audit.workspace.snapshot.snapshot_id,
        "visible_count": len(visible), "visible_term_df": dict(sorted(term_df.items())),
        "index_blocks": len(seen_paths), "maximum_block_fetches": limit,
        "ranking_contracts": sorted(item["name"] for item in catalog["ranking_contracts"]),
        "rankings": None,
    }
