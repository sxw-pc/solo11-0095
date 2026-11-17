# 规格说明

## 1. Workspace

workspace 是包含 `table.json`、`snapshots/`、`data/`、`deletes/`、`expected/` 的本地目录。所有清单中的文件路径必须是 workspace 内的 POSIX 相对路径。加载器拒绝绝对路径、空段、`.`、`..`、反斜杠、NUL、workspace 外解析结果和符号链接逃逸，并拒绝缺失的被引用文件、重复 ID、非法 JSON 与未知格式。

## 2. 表与模式

`table.json` 字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `format_version` | int | 可选，当前只支持 1 |
| `table_id` | string | 表 ID，非空 |
| `table_name` | string | 展示名 |
| `rule_version` | string | 删除规则版本，示例为 `MOR-1.0` |
| `created_at` | string | 合成 ISO 时间 |
| `expected_visible` | string | 期望结果相对路径 |
| `schema` | object | `columns` 数组 |

每个 column 有 `name`、`type`、`nullable`。`type` 只允许 `string`、`long`、`date`；列名必须匹配 `[A-Za-z_][A-Za-z0-9_]*` 且唯一；列顺序即 ordinal。schema 必须包含非空 `long` 的 `_row_pos`。

`schema_hash = sha256(canonical_json([{"name","type","nullable"}...]))`，按列顺序计算，`canonical_json` 使用键排序、无空格、UTF-8。`date` 只接受 `YYYY-MM-DD`；`long` 接受整数或十进制数字字符串；可空列的空串/`null` 规范化为 `None`。

## 3. 快照与文件清单

快照字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `snapshot_id` | string | 全局唯一快照 ID |
| `sequence_number` | int | 快照序列号，>=0 |
| `schema_hash` | string | 必须与表模式哈希一致 |
| `created_at` | string | ISO 时间 |
| `data_files` | array | 数据文件清单 |
| `delete_files` | array | 删除文件清单 |

数据文件条目：`file_id`、`path`、`format`（`csv`/`jsonl`）、`row_count`、`sha256`、`sequence_number`、`schema_hash`。

删除文件条目：`file_id`、`path`、`delete_kind`（`position`/`equality`）、`sequence_number`、`schema_hash`，可选 `sha256`、`target_file_id`、`equality_columns`。位置删除可对单个条目声明 `file_id`；等值删除可声明目标数据文件来限制作用域，否则作用于所有满足序列号条件的数据文件。

## 4. 数据格式与行身份

CSV 首行为表头，必须含 `_row_pos`，可选 `_row_id`，其余列对应 schema。JSONL 每行一个对象，同样必须含 `_row_pos`。行身份为 `(file_id, row_pos)`；`_row_pos` 必须是非负整数。业务主键 `_row_id` 可选，但重复会被报告。

行位置在同一文件内必须唯一。数据文件按 `file_id`、`row_pos` 排序；空行、非法 JSON、非法类型、缺失/额外列、行数不一致、重复位置、重复 `_row_id` 都产生 finding，冲突不中断后续行读取。身份非法或重复位置的行不进入标准化行集合。

期望文件 `expected_visible` 是 CSV，至少包含 `file_id`、`row_pos`、`expected_visible`；`expected_visible` 接受 `true/false/1/0/yes/no/visible/hidden`（大小写不敏感），可选 `_row_id`。

## 5. 序列号、哈希与校验和

- 数据文件序列号表示该文件数据的版本序号。
- 有目标文件的删除（位置删除或带 `target_file_id` 的等值删除）仅当 `delete_sequence >= data_sequence` 时应用；小于则报告 `STALE_SEQUENCE` 并忽略。
- 等值删除只作用于 `data_sequence <= delete_sequence` 的数据文件；相等时应用。未声明目标时没有单一目标文件可判过期。
- 数据文件 `sha256` 必须与文件字节一致；删除文件可选 `sha256`，声明后同样校验。
- 快照、数据文件、删除文件声明的 schema 哈希都与表 schema 比较；删除文件不一致时整文件不应用。
- workspace 指纹由 `table.json`、选中的快照、所有被引用数据/删除文件与期望文件的相对路径和内容哈希排序计算。`run_key = sha256(canonical_json({workspace_fingerprint, snapshot_id, rule_version}))`。

## 6. Finding 错误码

严重级别分为 `error` 和 `warning`；除下表标注 warning 的代码外均为 error。finding 按 `(file, row, code, message)` 稳定排序并赋 ordinal。

| 错误码 | 级别 | 含义 |
| --- | --- | --- |
| `WORKSPACE_INVALID` | error | workspace 不存在或不是目录 |
| `PATH_ESCAPE` | error | 绝对/上跳/反斜杠/符号链接逃逸 |
| `PATH_MISSING` | error | 被引用文件缺失 |
| `SNAPSHOT_NOT_FOUND` | error | 无快照或指定快照不存在 |
| `JSON_INVALID` | error | 清单 JSON 解析失败 |
| `FILE_DECODE_ERROR` | error | 文本解码或读取失败 |
| `MANIFEST_MISSING_FIELD` | error | 清单必需字段缺失 |
| `MANIFEST_TYPE_ERROR` | error | 清单字段类型或取值非法 |
| `SCHEMA_INVALID` | error | 模式列非法 |
| `DUPLICATE_COLUMN` | error | 列名重复 |
| `DUPLICATE_ID` | error | 快照/文件 ID 重复 |
| `UNKNOWN_FORMAT` | error | 未知数据文件格式 |
| `UNKNOWN_DELETE_KIND` | error | 未知删除类型 |
| `SCHEMA_HASH_MISMATCH` | error | 快照/数据文件模式哈希不一致 |
| `DELETE_SCHEMA_MISMATCH` | error | 删除文件模式哈希不一致，整文件不应用 |
| `FILE_HASH_MISMATCH` | error | 文件 sha256 与清单不一致 |
| `ROW_COUNT_MISMATCH` | error | 数据文件行数与清单不一致 |
| `MISSING_COLUMN` | error | 数据/期望文件缺少列或字段 |
| `EXTRA_COLUMN` | error | 数据/期望文件出现未知列或字段 |
| `INVALID_VALUE` | error | 字段不符合模式类型 |
| `INVALID_ROW` | error | 行结构非法 |
| `INVALID_JSON_LINE` | error | JSONL 行解析失败 |
| `BLANK_LINE` | warning | 空行 |
| `DUPLICATE_ROW_POSITION` | error | 同一文件内 row_pos 重复 |
| `DUPLICATE_ROW_ID` | warning | `_row_id` 重复 |
| `ORPHAN_DELETE_TARGET` | warning | 删除目标文件不存在或目标不一致 |
| `OUT_OF_RANGE_POSITION` | warning | 位置删除越界 |
| `DUPLICATE_POSITION_DELETE` | warning | 同一目标位置重复删除 |
| `STALE_SEQUENCE` | warning | 删除序列号早于目标数据文件 |
| `EQUALITY_NO_MATCH` | warning | 等值删除无匹配行 |
| `DUPLICATE_EQUALITY_DELETE` | warning | 同一删除文件内重复等值键 |
| `AMBIGUOUS_EQUALITY_COLUMNS` | warning | 等值列缺失、重复、未声明或不在模式中 |
| `DELETE_ENTRY_INVALID` | error | 删除条目结构或键值非法 |
| `DUPLICATE_EXPECTED_ROW` | error | 期望文件行重复 |
| `EXPECTED_INVALID` | error | 期望文件行或文件非法 |
| `MISSING_VISIBLE` | error | 期望可见但实际不可见 |
| `UNEXPECTED_VISIBLE` | error | 期望不可见但实际可见 |
| `EXTRA_EXPECTED_ROW` | error | 期望文件包含数据中不存在的行 |
| `MISSING_EXPECTED_ROW` | error | 数据行未被期望文件覆盖 |
| `ROW_NOT_FOUND` | error | explain 未找到目标行 |
| `LEDGER_CHECK_FAILED` | error | 台账缺失、损坏或校验失败 |
| `REPAIR_PLAN_INVALID` | error | 修复计划文件缺失、非法或结构不完整 |
| `REPAIR_PLAN_MISMATCH` | error | 修复计划与当前审计上下文（指纹/快照/规则版本）不一致 |
| `CLI_USAGE` | error | 命令行参数非法 |

## 7. 退出码

| 码 | 含义 |
| ---: | --- |
| 0 | 成功；无期望可见性差异（plan-repair：预演可行） |
| 2 | 审计完成；存在期望差异（plan-repair：预演不可行） |
| 3 | 输入、路径、台账或参数错误 |
| 4 | 未预期内部错误 |

`doctor` 成功返回 0；`audit` 存在差异返回 2；`verify-ledger` 校验失败返回 3；`explain` 行不存在返回 3；`plan-repair` 预演不可行返回 2。

## 8. 修复预演（plan-repair）

`plan-repair --workspace W [--snapshot S] [--scope entry|file] [--out DIR]` 输出 `repair_plan.json` 与 `repair_plan.md`。计划只是候选处置预演，不修改任何源文件。

- **证据图**：只对 `applicable` 且无 `conflict_code` 的删除条目建立“条目 → 命中行”映射；命中判定与可见性计算共用同一套位置/等值语义。无效、过期、重复、孤儿、模式不符的条目进入 `excluded_entries`，不参与处置。
- **动作**：`suspend_entry`（暂停指定 delete_file_id/ordinal）、`quarantine_file`（隔离整份删除文件，file scope）、`compensate_position`（为受影响但 `expected_visible=false` 的行建议新的精确位置删除；目标文件必须存在、位置必须在声明行数内、序列号取 `max(快照序列号, 目标数据序列号)` 以满足不过期）。
- **预演**：每个候选计划在内存中重跑删除适用 → 可见性 → 对账。可行的充要条件：所有有效期望行达到预期可见性，且未列入期望的行可见性与基线一致。`EXTRA_EXPECTED_ROW`、未列入期望的行被波及、预演后仍不达标的期望行都会产生有证据的冲突，计划标记不可行。
- **最小性**：同一 scope 下按“处置的已有删除条目数（file scope 为隔离文件数）、补偿条目数、稳定 file_id/ordinal 顺序”排序；可行时逐一移除每个动作（entry 为单条目、file 为整份文件）并重演，任一动作移除后仍可行则 `minimal=false`。
- **确定性**：计划 JSON 键排序、固定缩进；摘要无论可行与否字节稳定；`plan_key` 由 scope、动作集合、快照、指纹与差异计数的内容哈希生成。
- **引用**：`audit --repair-plan repair_plan.json` 校验 `workspace_fingerprint`、`snapshot_id`、`rule_version` 一致后，把计划摘要嵌入 `report.json`/`report.md`/`report.html` 与台账 `repair_plans` 表；不一致返回 `REPAIR_PLAN_MISMATCH`（退出码 3）。

## 9. 确定性

JSON 输出使用 `sort_keys=True`、固定缩进和 UTF-8；Markdown/HTML 表格按稳定排序；运行时间只来自 `--run-at`，默认 `2024-06-01T00:00:00Z`；不生成随机数；HTML 不引用 CDN、外部字体、脚本或网络资源。报告写入采用临时文件替换。
