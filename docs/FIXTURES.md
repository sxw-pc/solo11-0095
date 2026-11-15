# Fixture 说明

所有内容均为合成数据，仅供离线测试。规则版本 `MOR-1.0`，快照 `snap_001` 序列号 5。

## 1. 文件用途

| 文件 | 用途 |
| --- | --- |
| `table.json` | 表 ID、规则版本、期望结果相对路径、6 列 schema |
| `snapshots/snap_001.json` | 快照 5、3 个数据文件、4 个删除文件、哈希与序列号 |
| `data/base_001.csv` | 5 行 CSV，序列号 2；被有效位置删除和等值删除命中 |
| `data/base_002.csv` | 4 行 CSV，序列号 3；被等值删除命中 |
| `data/base_003.jsonl` | 5 行 JSONL，序列号 5；验证等值删除序列边界与过期位置删除 |
| `deletes/pos_010.jsonl` | 2 条有效位置删除（`base_001:1`、`:4`） |
| `deletes/pos_011.jsonl` | 序列号 2 对 `base_003` 的过期删除；含重复、越界、孤儿目标 |
| `deletes/eq_020.jsonl` | `region=north` 等值删除；含重复键与无匹配键 |
| `deletes/eq_021.jsonl` | schema 哈希错误且 `department` 不在模式中 |
| `expected/visible_snap_001.csv` | 14 行的期望可见布尔值，与规则计算结果一致 |

`base_001` 只有 `R0004` 可见；`base_002` 的 `R0007/R0008` 可见；`base_003` 全部可见。总可见 8 行、不可见 6 行。8 条 finding 是故意植入的删除向量异常，但期望结果无差异，因此 `demo` 退出码为 0。

## 2. 覆盖场景

正常场景：CSV/JSONL 读取、位置删除、等值删除、跨文件等值作用域、序列号相等、过期删除忽略、行级决定链、期望对账、报告和台账幂等。

异常场景：`ORPHAN_DELETE_TARGET`、`OUT_OF_RANGE_POSITION`、`DUPLICATE_POSITION_DELETE`、`STALE_SEQUENCE`、`EQUALITY_NO_MATCH`、`DUPLICATE_EQUALITY_DELETE`、`DELETE_SCHEMA_MISMATCH`、`AMBIGUOUS_EQUALITY_COLUMNS`。

测试还会临时构造路径逃逸、哈希不一致、行数不一致、非法类型、空行、重复行、期望差异、台账篡改等临时 fixture，不污染仓库样例。

## 3. 新增一个小 fixture

1. 在 `expected_visible` 指向的 CSV 中覆盖所有数据行；`file_id,row_pos,expected_visible` 必填，`_row_id` 可选。
2. 在 `data/` 新增 CSV 或 JSONL 文件，确保 `_row_pos` 唯一且类型符合 schema。
3. 在 `snapshots/` 新增快照，或修改现有快照，登记 `file_id`、路径、格式、`row_count`、文件 sha256、`sequence_number`、schema 哈希。
4. 新增删除 JSONL：位置条目为 `{"file_id","row_pos","reason"}`，等值条目为 `{"key":{...},"reason"}`；在清单声明 `delete_kind`、`sequence_number`、schema 哈希及可选的 `target_file_id`/`equality_columns`/sha256。
5. 重新计算文件 sha256 与 schema 哈希后更新清单；运行 `python -m lakedel doctor --workspace <workspace>` 与 `audit`，确认 finding 与期望差异符合设计。
6. 若新增稳定报告，更新 `tests/golden/` 并运行全部 `unittest`。

建议使用 `tests/support.py` 的 `copy_lake()` 在临时目录修改 fixture，避免影响 golden。所有文件应保持 300 行以内，示例值必须合成且不含真实信息。
