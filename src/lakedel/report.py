"""JSON、Markdown 与自包含 HTML 报告生成。"""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path
from typing import Any

from . import __version__
from .errors import finding_dicts
from .models import AuditResult
from .util import atomic_write_text, sha256_text
from .visibility import certainty_by_file, certainty_counts, visibility_by_file

LIMITATIONS = [
    "只支持本项目定义的本地 CSV/JSONL fixture 与 JSON 清单，不读取 Parquet、ORC 或真实湖表。",
    "输入文件哈希不符时不修复、不改写文件；受损内容仅作调查证据，相关行降级为待核定。",
    "不实现 Iceberg、Delta Lake、Hudi 的协议、快照时间旅行、compaction 或模式演进。",
    "不会修改任何数据文件或删除文件；所有建议均为说明性输出。",
    "类型系统仅覆盖 string、long、date，完成最小格式校验，不做数据库级类型强制。",
    "等值删除只比较规范化后的字段值；缺失字段不参与匹配，空值按显式 null/空串规则处理。",
    "报告只包含摘要、finding、差异和决定链摘要，逐行完整链保存在 SQLite 台账。",
]

REPORT_FILES = {"json": "report.json", "md": "report.md", "html": "report.html"}


def _row_visibility(decisions: tuple[Any, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in decisions:
        rows.append(
            {
                "file_id": item.file_id,
                "row_pos": item.row_pos,
                "row_id": item.row_id,
                "visible": item.visible,
                "provisional_visible": item.visible,
                "certainty": item.certainty,
                "certain_visible": item.certain_visible,
                "certain_hidden": item.certain_hidden,
                "indeterminate": item.indeterminate,
                "trusted_position_hits": list(item.trusted_position_hits),
                "trusted_equality_hits": list(item.trusted_equality_hits),
                "suspect_position_hits": list(item.suspect_position_hits),
                "suspect_equality_hits": list(item.suspect_equality_hits),
                "untrusted_sources": list(item.untrusted_sources),
                "untrusted_equality_sources": list(item.untrusted_equality_ids),
                "untrusted_equality_rows": [
                    list(pair) for pair in item.untrusted_equality_rows
                ],
                "trusted_interval": (
                    [False, True]
                    if item.indeterminate
                    else [item.certain_visible, item.certain_visible]
                ),
                "row_identity_trusted": item.row_identity_trusted,
                "reason_code": item.reason_code,
                "decision_chain": list(item.chain),
            }
        )
    return rows


def build_report(
    result: AuditResult, ledger_path: str, view: str = "publish"
) -> dict[str, Any]:
    workspace = result.workspace
    findings = finding_dicts(result.findings)
    finding_counts = Counter(item.code for item in result.findings)
    integrity = {
        "data_files": [item.to_dict() for item in workspace.data_integrity],
        "delete_files": [item.to_dict() for item in result.delete_integrity],
        "data_trusted": all(item.trusted for item in workspace.data_integrity),
        "delete_trusted": all(item.trusted for item in result.delete_integrity),
    }
    certainty = certainty_counts(result.decisions)
    outcome = (
        result.outcome.to_dict()
        if result.outcome is not None
        else {
            "publishable": False,
            "publish_reason": "NOT_PUBLISHABLE_INDETERMINATE",
            "blockers": [],
            "indeterminate_rows": [],
            "expected_mismatches": [],
        }
    )
    conclusion = dict(outcome)
    conclusion["authorized_use"] = view
    conclusion["provisional_allowed"] = view == "investigate"
    # 调查视图可以列暂定结果，但永远不能标记为可发布。
    conclusion["publishable"] = bool(outcome["publishable"]) and view == "publish"
    if view != "publish":
        conclusion["publish_reason"] = "INVESTIGATION_ONLY"
    return {
        "project": {
            "name": "行删镜",
            "package": "lakedel",
            "version": __version__,
            "rule_version": result.rule_version,
        },
        "run": {
            "run_key": result.run_key,
            "run_at": result.run_at,
            "workspace_fingerprint": workspace.fingerprint,
        },
        "snapshot": {
            "table_id": workspace.table.table_id,
            "table_name": workspace.table.table_name,
            "snapshot_id": workspace.snapshot.snapshot_id,
            "sequence_number": workspace.snapshot.sequence_number,
            "schema_hash": workspace.table.schema.schema_hash,
            "created_at": workspace.snapshot.created_at,
            "data_files": result.stats["data_files"],
            "delete_files": result.stats["delete_files"],
        },
        "summary": dict(result.stats),
        "violations": {
            "findings": len(findings),
            "by_code": dict(sorted(finding_counts.items())),
            "errors": result.stats["finding_errors"],
            "warnings": result.stats["finding_warnings"],
        },
        "findings": findings,
        "diffs": [item.to_dict() for item in result.diffs],
        "diff_summary": dict(result.diff_summary),
        "visibility_summary": {
            "reason_counts": dict(result.reason_counts),
            "by_file": visibility_by_file(result.decisions),
            "certainty_by_file": certainty_by_file(result.decisions),
        },
        "certainty_summary": {
            **certainty,
            "conserved": (
                certainty["certain_visible"]
                + certainty["certain_hidden"]
                + certainty["indeterminate"]
                == certainty["rows"]
            ),
        },
        "integrity": integrity,
        "conclusion": conclusion,
        "row_visibility": _row_visibility(result.decisions),
        "ledger_path": ledger_path,
        "limitations": list(LIMITATIONS),
    }


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _publish_banner(report: dict[str, Any]) -> str:
    conclusion = report["conclusion"]
    if conclusion["authorized_use"] != "publish":
        return (
            "调查视图：以下布尔结果仅为基于当前可解析内容的暂定值，"
            "不可标记为可发布，必须结合可信区间与来源人工复核。"
        )
    if conclusion["publishable"]:
        return "发布核验：全部确定态与期望一致且无待核定行，结果可发布。"
    reasons = ", ".join(conclusion["blockers"]) or conclusion["publish_reason"]
    return f"发布核验：不可发布，存在待核定或确定差异（{reasons}）。"


def _certainty_table(report: dict[str, Any]) -> list[str]:
    certainty = report["certainty_summary"]
    lines = [
        "",
        "## 三态与暂定结果",
        "",
        "| 指标 | 值 |",
        "| --- | ---: |",
        f"| certain_visible | {certainty['certain_visible']} |",
        f"| certain_hidden | {certainty['certain_hidden']} |",
        f"| indeterminate | {certainty['indeterminate']} |",
        f"| provisional_visible | {certainty['provisional_visible']} |",
        f"| provisional_hidden | {certainty['provisional_hidden']} |",
        f"| conserved | {str(certainty['conserved']).lower()} |",
    ]
    pending = [item for item in report["row_visibility"] if item["indeterminate"]]
    lines.extend(["", "### 待核定行", ""])
    if pending:
        lines.extend(
            [
                "| file_id | row_pos | 暂定可见 | 可信区间 | 可疑来源 | 可信命中 |",
                "| --- | ---: | --- | --- | --- | --- |",
            ]
        )
        for item in pending[:20]:
            lines.append(
                f"| {_cell(item['file_id'])} | {item['row_pos']} | "
                f"{item['provisional_visible']} | "
                f"{item['trusted_interval'][0]}..{item['trusted_interval'][1]} | "
                f"{_cell(','.join(item['untrusted_sources']) or '-')} | "
                f"{_cell(','.join(item['trusted_position_hits'] + item['trusted_equality_hits']) or '-')} |"
            )
    else:
        lines.append("无待核定行。")
    lines.extend(["", "## 来源完整性", "", "| 类型 | file_id | 状态 | 声明 SHA | 实际 SHA |", "| --- | --- | --- | --- | --- |"])
    sources = report["integrity"]["data_files"] + report["integrity"]["delete_files"]
    for item in sources:
        lines.append(
            f"| {_cell(item['source_kind'])} | {_cell(item['file_id'])} | "
            f"{_cell(item['status'])} | {_cell(item['declared_sha256'] or '-')} | "
            f"{_cell(item['actual_sha256'])} |"
        )
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    stats = report["summary"]
    diffs = report["diffs"]
    findings = report["findings"]
    conclusion = (
        "本次审计未发现期望可见性与引擎计算之间的差异。"
        if stats["diffs"] == 0
        else f"本次审计发现 {stats['diffs']} 处期望差异，需要人工复核。"
    )
    lines = [
        "# 行删镜审计报告",
        "",
        "## 结论",
        "",
        conclusion,
        f"共读取 {stats['rows']} 行，预计可见 {stats['visible_rows']} 行，"
        f"不可见 {stats['hidden_rows']} 行；记录 {stats['findings']} 条 finding。",
        "",
        "## 统计",
        "",
        "| 指标 | 值 |",
        "| --- | ---: |",
    ]
    for key in (
        "data_files", "delete_files", "delete_operations", "applicable_deletes",
        "rows", "visible_rows", "hidden_rows", "findings", "diffs",
    ):
        lines.append(f"| {key} | {stats[key]} |")
    lines.extend(['', _publish_banner(report)])
    lines.extend(_certainty_table(report))
    lines.extend(
        [
            "",
            "## 问题分类",
            "",
            "| 错误码 | 严重级别 | 数量 |",
            "| --- | --- | ---: |",
        ]
    )
    if findings:
        counts = Counter(item["code"] for item in findings)
        severity = {item["code"]: item["severity"] for item in findings}
        for code in sorted(counts):
            lines.append(f"| {_cell(code)} | {severity[code]} | {counts[code]} |")
    else:
        lines.append("| 无 | - | 0 |")
    lines.extend(["", "## 差异样例", ""])
    if diffs:
        lines.extend(
            [
                "| 分类 | file_id | row_pos | _row_id | 实际 | 期望 | 首个 finding | 决定链摘要 |",
                "| --- | --- | ---: | --- | --- | --- | --- | --- |",
            ]
        )
        for item in diffs[:20]:
            lines.append(
                "| {classification} | {file_id} | {row_pos} | {row_id} | "
                "{actual_visible} | {expected_visible} | {first_finding} | "
                "{decision_summary} |".format(
                    **{key: _cell(value) for key, value in item.items()}
                )
            )
    else:
        lines.append("未发现差异。")
    lines.extend(
        [
            "",
            "## 可见性摘要",
            "",
            "| reason_code | 行数 |",
            "| --- | ---: |",
        ]
    )
    for code, count in report["visibility_summary"]["reason_counts"].items():
        lines.append(f"| {_cell(code)} | {count} |")
    lines.extend(["", "## 限制说明", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.extend(["", f"台账：`{report['ledger_path']}`", ""])
    return "\n".join(lines)


def _html_table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{html.escape(str(item))}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(report: dict[str, Any]) -> str:
    stats = report["summary"]
    findings = report["findings"]
    diffs = report["diffs"]
    snapshot = report["snapshot"]
    style = (
        "body{font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;"
        "margin:0;background:#f5f7fa;color:#1f2937;line-height:1.55}"
        "main{max-width:1080px;margin:0 auto;padding:32px 20px}"
        "h1{font-size:28px;margin:0 0 8px}h2{font-size:19px;margin-top:28px}"
        ".card{background:#fff;border:1px solid #dbe2ea;border-radius:10px;"
        "padding:18px 20px;margin:14px 0;box-shadow:0 1px 2px #0001}"
        "table{width:100%;border-collapse:collapse;font-size:14px}"
        "th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #e5e7eb}"
        "th{background:#eef2f7}.warning{color:#92400e}.error{color:#991b1b}"
        "code{background:#eef2f7;padding:1px 4px;border-radius:4px}"
        ".muted{color:#6b7280}.num{text-align:right}"
    )
    stat_rows = [
        [key, stats[key]]
        for key in (
            "data_files", "delete_files", "delete_operations", "rows",
            "visible_rows", "hidden_rows", "findings", "diffs",
        )
    ]
    finding_rows = [
        [item["code"], item["severity"], item["file"],
         "-" if item["row"] is None else item["row"], item["message"]]
        for item in findings
    ]
    diff_rows = [
        [item["classification"], item["file_id"], item["row_pos"], item["row_id"] or "-",
         item["actual_visible"], item["expected_visible"]]
        for item in diffs[:20]
    ]
    reason_rows = [[code, count] for code, count in report["visibility_summary"]["reason_counts"].items()]
    conclusion = (
        "未发现期望可见性差异。"
        if stats["diffs"] == 0
        else f"发现 {stats['diffs']} 处期望可见性差异，需要人工复核。"
    )
    banner = _publish_banner(report)
    certainty = report["certainty_summary"]
    certainty_rows = [
        ["certain_visible", certainty["certain_visible"]],
        ["certain_hidden", certainty["certain_hidden"]],
        ["indeterminate", certainty["indeterminate"]],
        ["provisional_visible", certainty["provisional_visible"]],
        ["provisional_hidden", certainty["provisional_hidden"]],
        ["conserved", str(certainty["conserved"]).lower()],
    ]
    pending_rows = [
        [item["file_id"], item["row_pos"], item["provisional_visible"],
         f"{item['trusted_interval'][0]}..{item['trusted_interval'][1]}",
         ", ".join(item["untrusted_equality_sources"]) or "-",
         ", ".join(item["trusted_position_hits"] + item["trusted_equality_hits"]) or "-"]
        for item in report["row_visibility"] if item["indeterminate"]
    ][:20]
    source_rows = [
        [item["source_kind"], item["file_id"], item["status"],
         item["declared_sha256"] or "-", item["actual_sha256"]]
        for item in report["integrity"]["data_files"] + report["integrity"]["delete_files"]
    ]
    parts = [
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>行删镜审计报告 - {html.escape(snapshot['snapshot_id'])}</title>",
        f"<style>{style}</style></head><body><main>",
        "<h1>行删镜审计报告</h1>",
        f"<p class=\"muted\">表 {html.escape(snapshot['table_name'])} / 快照 "
        f"{html.escape(snapshot['snapshot_id'])} / 规则 {html.escape(report['project']['rule_version'])}</p>",
        f"<section class=\"card\" id=\"summary\"><h2>结论</h2><p>{html.escape(conclusion)}</p>",
        f"<p class=\"warning\">{html.escape(banner)}</p>",
        f"<p>工作区指纹 <code>{html.escape(report['run']['workspace_fingerprint'])}</code></p>",
        _html_table(["指标", "值"], stat_rows), "</section>",
        "<section class=\"card\" id=\"certainty\"><h2>三态与暂定结果</h2>",
        _html_table(["指标", "值"], certainty_rows),
        "<h3>待核定行</h3>",
        _html_table(
            ["file_id", "row_pos", "暂定可见", "可信区间", "可疑来源", "可信命中"],
            pending_rows,
        ) if pending_rows else "<p>无待核定行。</p>",
        "</section>",
        "<section class=\"card\" id=\"integrity\"><h2>来源完整性</h2>",
        _html_table(["类型", "file_id", "状态", "声明 SHA", "实际 SHA"], source_rows),
        "</section>",
        "<section class=\"card\" id=\"findings\"><h2>问题分类</h2>",
        _html_table(["错误码", "级别", "文件", "行", "说明"], finding_rows)
        if finding_rows else "<p>无 finding。</p>",
        "</section><section class=\"card\" id=\"diffs\"><h2>差异样例</h2>",
        _html_table(["分类", "file_id", "row_pos", "_row_id", "实际", "期望"], diff_rows)
        if diff_rows else "<p>未发现差异。</p>",
        "</section><section class=\"card\" id=\"visibility\"><h2>可见性摘要</h2>",
        _html_table(["reason_code", "行数"], reason_rows), "</section>",
        "<section class=\"card\" id=\"limitations\"><h2>限制说明</h2><ul>",
        "".join(f"<li>{html.escape(item)}</li>" for item in report["limitations"]),
        "</ul></section>",
        f"<p class=\"muted\">台账：<code>{html.escape(report['ledger_path'])}</code></p>",
        "<p class=\"muted\">本报告完全离线生成，不引用 CDN、外部字体或脚本。</p>",
        "</main></body></html>",
    ]
    return "\n".join(parts) + "\n"


def write_reports(
    out_dir: str | Path,
    result: AuditResult,
    formats: list[str],
    ledger_path: str,
    view: str = "publish",
) -> list[dict[str, Any]]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = build_report(result, ledger_path, view)
    artifacts: list[dict[str, Any]] = []
    for fmt in sorted(set(formats)):
        if fmt == "json":
            text = json.dumps(
                report, ensure_ascii=False, indent=2, sort_keys=True
            ) + "\n"
        elif fmt == "md":
            text = render_markdown(report)
        elif fmt == "html":
            text = render_html(report)
        else:
            raise ValueError(f"unknown report format: {fmt}")
        path = out / REPORT_FILES[fmt]
        atomic_write_text(path, text)
        artifacts.append(
            {"path": path, "format": fmt, "sha256": sha256_text(text)}
        )
    return artifacts
