"""Recreate the synthetic deletion-bearing search lake and block postings."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "examples/search_lake"
SCHEMA_HASH = "53aefe07e99bbe0bb0cb464919345a24921e4240279d994747727daf1e55ff97"


def write(path: str, content: str) -> str:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    target.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def payload(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


def generate() -> None:
    table = {
        "format_version": 1, "table_id": "tbl_search_001", "table_name": "合成搜索湖表",
        "rule_version": "MOR-1.0", "created_at": "2024-01-01T00:00:00Z",
        "expected_visible": "expected/visible.csv",
        "schema": {"columns": [
            {"name": "_row_id", "type": "string", "nullable": False},
            {"name": "_row_pos", "type": "long", "nullable": False},
            {"name": "category", "type": "string", "nullable": False},
            {"name": "region", "type": "string", "nullable": False},
            {"name": "amount", "type": "long", "nullable": False},
            {"name": "event_date", "type": "date", "nullable": True},
        ]},
    }
    write("table.json", payload(table))
    data_buffer, expected_buffer = io.StringIO(newline=""), io.StringIO(newline="")
    data_writer = csv.writer(data_buffer, lineterminator="\n")
    expected_writer = csv.writer(expected_buffer, lineterminator="\n")
    data_writer.writerow(["_row_id", "_row_pos", "category", "region", "amount", "event_date"])
    expected_writer.writerow(["file_id", "row_pos", "_row_id", "expected_visible"])
    documents = []
    postings: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    regions = ("north", "south", "east", "west")
    for pos in range(1024):
        number = pos + 1
        row_id = f"S{number:04d}"
        region = regions[pos % 4]
        data_writer.writerow([row_id, pos, "document", region, number, ""])
        expected_writer.writerow(["search_data", pos, row_id, "false" if pos < 32 else "true"])
        terms = ["lake"]
        if pos < 32 or number == 999:
            terms.append("repair")
        if number % 16 == 0 or number == 702:
            terms.append("audit")
        if number == 700:
            terms.extend(["audit", "audit"])
        documents.append({"row_id": row_id, "region": region, "terms": terms})
        for term, tf in Counter(terms).items():
            postings[term][pos // 32].append({"row_id": row_id, "tf": tf})
    data_hash = write("data/search.csv", data_buffer.getvalue())
    write("expected/visible.csv", expected_buffer.getvalue())
    deletes = "".join(payload({"file_id": "search_data", "row_pos": pos,
                               "reason": "synthetic deletion"}) for pos in range(32))
    delete_hash = write("deletes/old_repair.jsonl", deletes)
    snapshot = {
        "snapshot_id": "search_after", "sequence_number": 2,
        "schema_hash": SCHEMA_HASH, "created_at": "2024-06-01T00:00:00Z",
        "data_files": [{"file_id": "search_data", "path": "data/search.csv",
                        "format": "csv", "row_count": 1024, "sha256": data_hash,
                        "sequence_number": 1, "schema_hash": SCHEMA_HASH}],
        "delete_files": [{"file_id": "old_repair", "path": "deletes/old_repair.jsonl",
                          "delete_kind": "position", "sequence_number": 2,
                          "target_file_id": "search_data", "schema_hash": SCHEMA_HASH,
                          "sha256": delete_hash}],
    }
    write("snapshots/search_after.json", payload(snapshot))
    catalog = {
        "snapshot_id": "search_after", "documents": documents,
        "queries": [{"query_id": "repair_audit", "terms": ["repair", "audit"], "top_k": 2}],
        "query_budget": {"maximum_document_fetches": 64},
        "ranking_contracts": [
            {"name": "lexical", "required_regions": []},
            {"name": "regional", "required_regions": ["south"]},
        ],
    }
    write("search_catalog.json", payload(catalog))
    index = {"snapshot_id": "search_after", "block_size": 32,
             "postings": {term: [
                 {"block": block, "max_tf": max(item["tf"] for item in items),
                  "items": items}
                 for block, items in sorted(blocks.items())]
                 for term, blocks in sorted(postings.items())}}
    write("search_blocks.json", payload(index))


if __name__ == "__main__":
    generate()
