"""Validate a fixed block-posting index without serving a ranked query."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .search_corpus import inspect_search_corpus


def inspect_search_blocks(workspace: Path) -> dict[str, Any]:
    catalog_path = workspace / "search_catalog.json"
    index_path = workspace / "search_blocks.json"
    corpus = inspect_search_corpus(workspace, catalog_path)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if index.get("snapshot_id") != corpus["snapshot_id"] or index.get("block_size") != 32:
        raise ValueError("search index does not match audited snapshot")
    documents = catalog["documents"]
    positions = {item["row_id"]: position for position, item in enumerate(documents)}
    expected: dict[str, list[tuple[int, str, int]]] = {}
    for position, item in enumerate(documents):
        for term, tf in Counter(item["terms"]).items():
            expected.setdefault(term, []).append((position // 32, item["row_id"], tf))
    observed: dict[str, list[tuple[int, str, int]]] = {}
    postings = index.get("postings")
    if not isinstance(postings, dict):
        raise ValueError("search index postings are missing")
    for term, blocks in postings.items():
        seen_blocks: set[int] = set()
        for block in blocks:
            number = block.get("block")
            if not isinstance(number, int) or number in seen_blocks:
                raise ValueError("repeated or invalid index block")
            seen_blocks.add(number)
            items = block.get("items")
            if not isinstance(items, list) or not items:
                raise ValueError("empty index block")
            if block.get("max_tf") != max(item.get("tf", -1) for item in items):
                raise ValueError("index block upper bound mismatch")
            for item in items:
                row_id, tf = item.get("row_id"), item.get("tf")
                if row_id not in positions or positions[row_id] // 32 != number:
                    raise ValueError("index block points outside its row range")
                observed.setdefault(term, []).append((number, row_id, tf))
    if observed != expected:
        raise ValueError("index postings differ from catalog terms")
    limit = catalog.get("query_budget", {}).get("maximum_document_fetches")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit < len(documents):
        raise ValueError("query fetch budget must be below corpus size")
    return {
        "run_key": corpus["run_key"], "snapshot_id": corpus["snapshot_id"],
        "visible_count": corpus["visible_count"],
        "visible_term_df": corpus["term_document_frequency"],
        "index_blocks": sum(len(blocks) for blocks in postings.values()),
        "maximum_document_fetches": limit,
        "ranking_contracts": sorted(item["name"] for item in catalog["ranking_contracts"]),
        "rankings": None,
    }
