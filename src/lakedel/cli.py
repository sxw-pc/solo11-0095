"""命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .demo import run_demo
from .engine import execute_audit
from .errors import AuditError
from .ledger import verify_ledger
from .repair import SCOPES, plan_repair, write_plan
from .service import produce_outputs
from .util import FIXED_RUN_AT, pretty_json

DEFAULT_WORKSPACE = "examples/lake"
FORMATS = ("json", "md", "html")


class CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AuditError("CLI_USAGE", message)


def _formats(value: str) -> list[str]:
    items = sorted({part.strip() for part in value.split(",") if part.strip()})
    if not items or any(item not in FORMATS for item in items):
        raise AuditError("CLI_USAGE", "--format 仅支持 json,md,html")
    return items


def _doctor(args: argparse.Namespace) -> int:
    result = execute_audit(args.workspace, args.snapshot, FIXED_RUN_AT)
    stats = result.stats
    warnings = [item for item in result.findings if item.severity == "warning"]
    payload = {
        "workspace": str(Path(args.workspace)),
        "snapshot_id": result.workspace.snapshot.snapshot_id,
        "data_files": stats["data_files"],
        "delete_files": stats["delete_files"],
        "rows": stats["rows"],
        "delete_operations": stats["delete_operations"],
        "expected_visible": stats["visible_rows"],
        "findings": stats["findings"],
        "warnings": len(warnings),
        "warning_codes": sorted({item.code for item in warnings}),
        "workspace_fingerprint": result.workspace.fingerprint,
    }
    if args.json:
        print(pretty_json(payload), end="")
    else:
        print(f"workspace: {payload['workspace']}")
        print(f"snapshot: {payload['snapshot_id']}")
        print(f"data_files: {payload['data_files']}")
        print(f"delete_files: {payload['delete_files']}")
        print(f"rows: {payload['rows']}")
        print(f"delete_operations: {payload['delete_operations']}")
        print(f"expected_visible: {payload['expected_visible']}")
        print(f"findings: {payload['findings']} (warnings={payload['warnings']})")
        codes = ", ".join(payload["warning_codes"]) or "none"
        print(f"warning_codes: {codes}")
    return 0


def _audit(args: argparse.Namespace) -> int:
    formats = _formats(args.format)
    out = Path(args.out)
    db = Path(args.db) if args.db else out / "ledger.sqlite"
    result, artifacts, ledger = produce_outputs(
        args.workspace, out, formats, db, args.snapshot, args.run_at,
        repair_plan_path=args.repair_plan,
    )
    print(f"snapshot: {result.workspace.snapshot.snapshot_id}")
    print(f"rows: {result.stats['rows']} visible={result.stats['visible_rows']} "
          f"hidden={result.stats['hidden_rows']}")
    print(f"findings: {result.stats['findings']} diffs: {result.stats['diffs']}")
    print(f"ledger: {ledger}")
    for artifact in artifacts:
        print(f"{artifact['format']}: {artifact['path']} {artifact['sha256']}")
    return 2 if result.stats["diffs"] else 0


def _plan_repair(args: argparse.Namespace) -> int:
    plan = plan_repair(args.workspace, args.snapshot, args.scope, args.run_at)
    artifacts = write_plan(args.out, plan)
    summary = plan["summary"]
    print(f"snapshot: {plan['run']['snapshot_id']}")
    print(f"scope: {summary['scope']} feasible: {summary['feasible']}")
    print(
        f"actions: {summary['actions']} "
        f"(suspended={summary['suspended_entries']} "
        f"quarantined={summary['quarantined_files']} "
        f"compensations={summary['compensation_entries']})"
    )
    print(
        f"residual_diffs: {summary['residual_diffs']} "
        f"conflicts: {summary['conflicts']} minimal: {summary['minimal']}"
    )
    print(f"plan_key: {summary['plan_key']}")
    for artifact in artifacts:
        print(f"{artifact['format']}: {artifact['path']} {artifact['sha256']}")
    return 0 if summary["feasible"] else 2


def _explain(args: argparse.Namespace) -> int:
    if args.row_id and (args.file_id or args.row_pos is not None):
        raise AuditError("CLI_USAGE", "使用 --row-id 或 --file-id/--row-pos，二选一")
    if not args.row_id and not (args.file_id and args.row_pos is not None):
        raise AuditError("CLI_USAGE", "必须提供 --row-id 或 --file-id 与 --row-pos")
    result = execute_audit(args.workspace, args.snapshot, FIXED_RUN_AT)
    if args.row_id:
        decision = next(
            (item for item in result.decisions if item.row_id == args.row_id), None
        )
    else:
        decision = next(
            (
                item for item in result.decisions
                if item.file_id == args.file_id and item.row_pos == args.row_pos
            ),
            None,
        )
    if decision is None:
        raise AuditError("ROW_NOT_FOUND", "未找到指定行")
    related = [
        item.to_dict()
        for item in result.findings
        if item.file == decision.file_id
        and (item.row == decision.row_pos or item.row is None)
    ]
    payload: dict[str, Any] = {
        "row": {
            "file_id": decision.file_id,
            "row_pos": decision.row_pos,
            "row_id": decision.row_id,
        },
        "visible": decision.visible,
        "reason_code": decision.reason_code,
        "position_hits": list(decision.position_hits),
        "equality_hits": list(decision.equality_hits),
        "stale_hits": list(decision.stale_hits),
        "decision_chain": list(decision.chain),
        "related_findings": related,
    }
    print(pretty_json(payload), end="")
    return 0


def _verify(args: argparse.Namespace) -> int:
    report = verify_ledger(args.db)
    print(pretty_json(report), end="")
    return 0 if report["ok"] else 3


def _demo(args: argparse.Namespace) -> int:
    formats = _formats(args.format)
    result, artifacts, ledger, workspace = run_demo(args.out, formats, args.run_at)
    out = Path(args.out) if args.out else Path(__file__).resolve().parents[2] / "out" / "demo"
    print("行删镜演示审计完成")
    print(f"workspace: {workspace}")
    print(f"rows: {result.stats['rows']} visible={result.stats['visible_rows']}")
    print(f"findings: {result.stats['findings']} diffs: {result.stats['diffs']}")
    print(f"reports: {', '.join(item['format'] for item in artifacts)}")
    print(f"out: {out}")
    print(f"ledger: {ledger}")
    return 2 if result.stats["diffs"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = CliParser(prog="lakedel", description="行删镜：湖表行级删除可见性核验")
    parser.add_argument("--version", action="version", version=f"lakedel {__version__}")
    subs = parser.add_subparsers(dest="command")
    doctor = subs.add_parser("doctor", help="检查 workspace 并给出预计结果")
    doctor.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    doctor.add_argument("--snapshot")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_doctor)
    audit = subs.add_parser("audit", help="执行审计并输出报告与台账")
    audit.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    audit.add_argument("--snapshot")
    audit.add_argument("--out", default="out/audit")
    audit.add_argument("--db")
    audit.add_argument("--format", default="json,md,html")
    audit.add_argument("--run-at", default=FIXED_RUN_AT)
    audit.add_argument("--repair-plan", help="可选：plan-repair 生成的 repair_plan.json，"
                                             "校验上下文后嵌入报告与台账")
    audit.set_defaults(func=_audit)
    plan = subs.add_parser("plan-repair", help="可见性差异的反事实修复预演（只输出计划）")
    plan.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    plan.add_argument("--snapshot")
    plan.add_argument("--scope", default="entry", choices=SCOPES)
    plan.add_argument("--out", default="out/plan")
    plan.add_argument("--run-at", default=FIXED_RUN_AT)
    plan.set_defaults(func=_plan_repair)
    explain = subs.add_parser("explain", help="解释单行最终可见性")
    explain.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    explain.add_argument("--snapshot")
    explain.add_argument("--row-id")
    explain.add_argument("--file-id")
    explain.add_argument("--row-pos", type=int)
    explain.set_defaults(func=_explain)
    verify = subs.add_parser("verify-ledger", help="校验 SQLite 台账")
    verify.add_argument("--db", required=True)
    verify.set_defaults(func=_verify)
    demo = subs.add_parser("demo", help="使用仓库 fixture 跑完整演示")
    demo.add_argument("--out")
    demo.add_argument("--format", default="json,md,html")
    demo.add_argument("--run-at", default=FIXED_RUN_AT)
    demo.set_defaults(func=_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if not hasattr(args, "func"):
            parser.print_help()
            return 0
        return args.func(args)
    except AuditError as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        return 3
    except BrokenPipeError:
        return 3
    except Exception as exc:  # noqa: BLE001 - CLI 边界必须转换为稳定退出码
        print(f"[INTERNAL_ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4
