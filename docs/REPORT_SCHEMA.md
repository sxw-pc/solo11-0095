# 报告与台账结构

## 1. JSON 报告

文件名 `report.json`，UTF-8、两空格缩进、键排序、末尾换行。顶层字段：

| 字段 | 说明 |
| --- | --- |
| `project` | `name`、`package`、`version`、`rule_version` |
| `run` | `run_key`、`run_at`、`workspace_fingerprint` |
| `snapshot` | 表 ID/名称、快照 ID、序列号、模式哈希、创建时间、数据/删除文件数 |
| `summary` | 数据/删除文件数、操作数、可应用删除数、行数、可见/不可见行、期望行数、finding 数、error/warning 数、差异数 |
| `violations` | finding 总数、按 code 计数、error/warning 计数 |
| `findings` | 按稳定顺序排列，含 `ordinal/code/severity/file/row/message/evidence` |
| `diffs` | 期望差异明细 |
| `diff_summary` | 四种差异分类与 `total` |
| `visibility_summary` | `reason_counts`、按文件的 rows/visible/hidden、按文件的 `certainty_by_file` |
| `certainty_summary` | `certain_visible`/`certain_hidden`/`indeterminate`、`provisional_visible`/`provisional_hidden`、`rows`、`conserved` |
| `integrity` | 数据/删除来源的声明与实际 SHA、`trusted`、`mismatch` |
| `conclusion` | `authorized_use`（publish/investigate）、`publishable`、`publish_reason`、`blockers`、`indeterminate_rows`、`expected_mismatches` |
| `row_visibility` | 每行三态、暂定布尔、可信/可疑命中、`untrusted_sources`、`untrusted_equality_rows`、`trusted_interval`、决定链 |
| `ledger_path` | 调用方传入的台账路径 |
| `limitations` | 固定限制说明 |

`diffs` 每项含 `classification`、`file_id`、`row_pos`、`row_id`、`actual_visible`、`expected_visible`、`first_finding`、`decision_summary`。`decision_summary` 是 reason code 及命中来源的短摘要；完整链在台账 `row_visibility.decision_chain_json`。

## 2. Markdown 报告

文件名 `report.md`，章节固定为：`# 行删镜审计报告`、`## 结论`、`## 统计`、`## 问题分类`、`## 差异样例`、`## 可见性摘要`、`## 限制说明`。表格按稳定键排序，差异样例最多 20 条；无差异时明确写“未发现差异。”。台账路径附在末尾。

## 3. HTML 报告

文件名 `report.html`，必须自包含：所有样式在 `<style>` 内，不引用 CDN、外部字体、图片、脚本或样式表；不使用 `http://`、`https://`、`<link>`、`<script>`、`url(...)`。关键标记：

- `id="summary"`：结论、工作区指纹、统计表。
- `id="findings"`：finding 分类表。
- `id="diffs"`：差异样例。
- `id="visibility"`：reason code 计数。
- `id="certainty"`：三态/暂定结果与待核定行。
- `id="integrity"`：来源完整性表。
- `id="limitations"`：限制说明。

动态内容全部 HTML 转义；页面无 JavaScript、无随机数。

## 4. SQLite 台账

默认路径 `out/audit/ledger.sqlite`，可用 `--db` 指定。启用外键，写入在一个事务中完成。相同 `run_key` 重跑时更新原 run，删除并重建其子表记录，不新增 run。

```sql
CREATE TABLE runs (
  run_key TEXT PRIMARY KEY,
  workspace_fingerprint TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  run_at TEXT NOT NULL,
  status TEXT NOT NULL,              -- passed / completed_with_diff
  stats_json TEXT NOT NULL
);
CREATE TABLE artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_key TEXT NOT NULL REFERENCES runs(run_key) ON DELETE CASCADE,
  path TEXT NOT NULL,                -- 相对台账目录
  sha256 TEXT NOT NULL,
  format TEXT NOT NULL,
  UNIQUE(run_key, path)
);
CREATE TABLE findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_key TEXT NOT NULL REFERENCES runs(run_key) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  code TEXT NOT NULL,
  severity TEXT NOT NULL,
  file TEXT NOT NULL,
  row INTEGER,
  message TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  UNIQUE(run_key, ordinal)
);
CREATE TABLE row_visibility (
  run_key TEXT NOT NULL REFERENCES runs(run_key) ON DELETE CASCADE,
  file_id TEXT NOT NULL,
  row_pos INTEGER NOT NULL,
  row_id TEXT,
  visible INTEGER NOT NULL,
  provisional_visible INTEGER NOT NULL DEFAULT 0,
  certainty TEXT NOT NULL DEFAULT 'certain_visible',
  certain_visible INTEGER NOT NULL DEFAULT 0,
  certain_hidden INTEGER NOT NULL DEFAULT 0,
  indeterminate INTEGER NOT NULL DEFAULT 0,
  trusted_hits_json TEXT NOT NULL DEFAULT '[]',
  suspect_hits_json TEXT NOT NULL DEFAULT '[]',
  untrusted_sources_json TEXT NOT NULL DEFAULT '[]',
  trusted_interval_json TEXT NOT NULL DEFAULT '[false,false]',
  row_identity_trusted INTEGER NOT NULL DEFAULT 1,
  reason_code TEXT NOT NULL,
  decision_chain_json TEXT NOT NULL,
  PRIMARY KEY(run_key, file_id, row_pos)
);
CREATE TABLE source_integrity (
  run_key TEXT NOT NULL REFERENCES runs(run_key) ON DELETE CASCADE,
  source_kind TEXT NOT NULL,
  file_id TEXT NOT NULL,
  path TEXT NOT NULL,
  declared_sha256 TEXT,
  actual_sha256 TEXT NOT NULL,
  trusted INTEGER NOT NULL,
  mismatch INTEGER NOT NULL,
  PRIMARY KEY(run_key, source_kind, file_id)
);
CREATE TABLE outcome (
  run_key TEXT PRIMARY KEY REFERENCES runs(run_key) ON DELETE CASCADE,
  view TEXT NOT NULL,
  publishable INTEGER NOT NULL,
  publish_reason TEXT NOT NULL,
  blockers_json TEXT NOT NULL,
  indeterminate_rows_json TEXT NOT NULL,
  expected_mismatches_json TEXT NOT NULL,
  provisional_visible INTEGER NOT NULL,
  provisional_hidden INTEGER NOT NULL,
  certain_visible INTEGER NOT NULL,
  certain_hidden INTEGER NOT NULL,
  indeterminate INTEGER NOT NULL
);
CREATE TABLE diff_summary (
  run_key TEXT NOT NULL REFERENCES runs(run_key) ON DELETE CASCADE,
  classification TEXT NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY(run_key, classification)
);
```

`diff_summary` 写入四种分类及 `total`。`decision_chain_json` 是决定链数组，包含 `initial_visibility`、`data_source_untrusted`、可信/可疑位置与等值命中、`untrusted_delete_scope`、`stale_delete_ignored`、`final_visibility`（含 `certainty`）步骤。`outcome.view` 记录本次写入的授权用途。

## 5. verify-ledger

`verify-ledger --db PATH` 检查：所需表存在、外键无违规、至少一个 run、`(workspace_fingerprint, snapshot_id, rule_version)` 无重复、每个 run 的 row_visibility 行数/可见数/finding 数/差异总数与 `stats_json` 一致，三态之和等于行数，且存在 `outcome` 行、artifact 文件存在且 sha256 匹配。输出稳定 JSON，含 `ok`、`runs`、`checks`。
