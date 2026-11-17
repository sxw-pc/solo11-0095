# 完整性分层与三态证明

本工具不修复、不改写任何输入。当输入文件内容与清单声明不符时，它把该文件的内容降级为**调查证据**，并在逐行结果上给出三态与可信区间。

## 1. 来源完整性

| 来源 | 可信条件 | 不可信时的影响 |
| --- | --- | --- |
| 数据文件 | 实际 SHA256 等于清单 `sha256` | 行身份（`_row_id`、位置）与等值键不可作为确定证据，该行全部降级为 `indeterminate` |
| 删除文件 | 声明了 `sha256` 且实际值相等；未声明 `sha256` 时视为无哈希声明，不触发降级 | 其解析条目只作调查证据；在清单声明的作用域内仍可能命中任何行 |

完整性状态由 `fileio.data_integrity` 与 `delete_vectors._spec_integrity` 产生，附着到 `RowRecord` 与 `DeleteOperation`，驱动 `visibility.compute_visibility` 的证明链，并在 `reconcile.reconcile_certainty` 中决定发布核验结论。报告文字不是唯一出口。

`FILE_HASH_MISMATCH` 仍各产生一条 finding，`evidence` 中带 `trusted=false` 与 `impact`。损伤文件无法整行解析时，除 `INVALID_JSON_LINE` 外，其清单声明的目标与序列号仍用于保守作用域计算，防止受损删除被静默忽略。

## 2. 三态定义

对每一行计算：

1. **`certain_hidden`**：数据行身份可信，且至少一个**可信**位置/等值删除有效命中。
2. **`certain_visible`**：数据行身份可信，没有可信命中，且所有可能适用的可信与不可信删除都被排除。
3. **`indeterminate`**：其余情况。

“可能适用”指：删除未过期、清单声明的目标与序列号作用域覆盖该行。不可信删除的键值/坐标不可确认，因此作用域内所有行都不能排除；不可信数据行上的可信命中也不能证明 `hidden`，因为命中对象本身不可信。

暂定布尔结果 `provisional_visible` 仍按当前可解析内容计算，等于旧版 `visible`，用于调查视图。可信区间在确定态为单点，在 `indeterminate` 时为 `[false, true]`。

统计守恒：`certain_visible + certain_hidden + indeterminate == rows`，`provisional_visible + provisional_hidden == rows`；引擎在违反时抛错，台账 `verify-ledger` 也会复查。

## 3. 两种授权用途

| 用途 | 触发 | 结论 |
| --- | --- | --- |
| 发布核验 | `audit` 默认或 `--view publish` | 仅当每个 expected 行确定态与期望一致、无 `indeterminate`、无期望差异时才 `publishable=true`；否则 CLI 退出码 `2` |
| 调查视图 | `audit --view investigate`、`explain` | 可列暂定布尔结果，必须显示可信区间、可信命中与可疑来源；`publishable` 恒为 `false`，`publish_reason=INVESTIGATION_ONLY` |

`explain` 同时输出三态、可信区间、可信/可疑命中、`untrusted_sources`、`untrusted_equality_rows` 与 `publishable_row`。

## 4. 隔离 fixture

`tests/fixtures/integrity_delete`：

- `pos_trusted` 可信位置删除命中 A。
- `eq_damaged` 声明哈希与实际字节不符，按当前内容等值命中 A、B。
- A 由可信位置删除证明 `certain_hidden`；B 只有受损来源命中，暂定 hidden 但为 `indeterminate`。
- 发布核验阻止 B 通过；调查视图列出 B 的暂定 hidden 与 `[false, true]` 区间。

`tests/fixtures/integrity_data`：

- `bad.csv` 声明哈希与实际字节不符；`good.csv` 可信。
- `bad:0` 有可信位置删除命中，但数据身份不可信，仍为 `indeterminate`；`bad:1` 无删除命中也为 `indeterminate`（无法排除受损删除内容）。
- `good:0` 保持 `certain_hidden`。

两个 fixture 的 expected 均与暂定布尔一致，因此 `diff=0`；发布核验仍因 `indeterminate` 失败，演示“无差异不等于可发布”。

## 5. 输出位置

- JSON：`certainty_summary`、`integrity`、`conclusion`、`row_visibility`，以及 `diffs[].evidence` 中的三态。
- Markdown：`发布核验/调查视图` 横幅、三态表、待核定行表、来源完整性表。
- HTML：`id="certainty"`、`id="integrity"`。
- SQLite：`row_visibility` 的 `provisional_visible`、`certainty`、三态列、`trusted_hits_json`、`suspect_hits_json`、`untrusted_sources_json`、`trusted_interval_json`、`row_identity_trusted`；`source_integrity`；`outcome`。
