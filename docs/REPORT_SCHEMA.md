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
| `visibility_summary` | `reason_counts` 与按文件的 rows/visible/hidden |
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
  reason_code TEXT NOT NULL,
  decision_chain_json TEXT NOT NULL,
  PRIMARY KEY(run_key, file_id, row_pos)
);
CREATE TABLE diff_summary (
  run_key TEXT NOT NULL REFERENCES runs(run_key) ON DELETE CASCADE,
  classification TEXT NOT NULL,
  count INTEGER NOT NULL,
  PRIMARY KEY(run_key, classification)
);
```

`diff_summary` 写入四种分类及 `total`。`decision_chain_json` 是决定链数组，包含 `initial_visibility`、位置/等值命中、`stale_delete_ignored`、`final_visibility` 步骤。

## 5. verify-ledger

`verify-ledger --db PATH` 检查：所需表存在、外键无违规、至少一个 run、`(workspace_fingerprint, snapshot_id, rule_version)` 无重复、每个 run 的 row_visibility 行数/可见数/finding 数/差异总数与 `stats_json` 一致、artifact 文件存在且 sha256 匹配。输出稳定 JSON，含 `ok`、`runs`、`checks`。
