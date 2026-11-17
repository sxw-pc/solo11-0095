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
from .visibility import visibility_by_file

LIMITATIONS = [
    "只支持本项目定义的本地 CSV/JSONL fixture 与 JSON 清单，不读取 Parquet、ORC 或真实湖表。",
    "不实现 Iceberg、Delta Lake、Hudi 的协议、快照时间旅行、compaction 或模式演进。",
    "不会修改任何数据文件或删除文件；所有建议均为说明性输出。",
    "类型系统仅覆盖 string、long、date，完成最小格式校验，不做数据库级类型强制。",
    "等值删除只比较规范化后的字段值；缺失字段不参与匹配，空值按显式 null/空串规则处理。",
    "报告只包含摘要、finding、差异和决定链摘要，逐行完整链保存在 SQLite 台账。",
]

REPORT_FILES = {"json": "report.json", "md": "report.md", "html": "report.html"}


def build_report(result: AuditResult, ledger_path: str) -> dict[str, Any]:
    workspace = result.workspace
    findings = finding_dicts(result.findings)
    finding_counts = Counter(item.code for item in result.findings)
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
        },
        "ledger_path": ledger_path,
        "limitations": list(LIMITATIONS),
    }


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


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
    parts = [
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>行删镜审计报告 - {html.escape(snapshot['snapshot_id'])}</title>",
        f"<style>{style}</style></head><body><main>",
        "<h1>行删镜审计报告</h1>",
        f"<p class=\"muted\">表 {html.escape(snapshot['table_name'])} / 快照 "
        f"{html.escape(snapshot['snapshot_id'])} / 规则 {html.escape(report['project']['rule_version'])}</p>",
        f"<section class=\"card\" id=\"summary\"><h2>结论</h2><p>{html.escape(conclusion)}</p>",
        f"<p>工作区指纹 <code>{html.escape(report['run']['workspace_fingerprint'])}</code></p>",
        _html_table(["指标", "值"], stat_rows), "</section>",
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
) -> list[dict[str, Any]]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = build_report(result, ledger_path)
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
