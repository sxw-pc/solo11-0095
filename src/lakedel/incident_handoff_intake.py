"""Static handoff preview for a synthetic row-delete audit finding."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import execute_audit
from .util import canonical_json, sha256_text


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview synthetic audit incident handoff")
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args(argv)
    data = json.loads(args.fixture.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[2]
    result = execute_audit(root / "examples/incident/lake")
    print(json.dumps(static_preview(data, result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
