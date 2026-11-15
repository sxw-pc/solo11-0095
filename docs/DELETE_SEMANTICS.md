# 删除语义（规则版本 MOR-1.0）

本工具只计算**可见性**，不修改数据。默认初始状态是“行可见”，然后按删除向量逐项决定是否可见。

## 1. 位置删除

位置删除由 `(target_file_id, row_pos)` 定位一行。清单可以声明 `target_file_id`，条目可以写 `file_id`；若两者都存在必须一致。

示例：`pos_010.jsonl` 声明目标 `base_001`，条目删除 `row_pos=1` 与 `row_pos=4`。`base_001` 的 `R0002/R0005` 最终不可见，其他行不受影响。目标为空、目标文件不存在、条目目标与清单目标不一致，均产生 `ORPHAN_DELETE_TARGET`，不应用。

## 2. 等值删除

等值删除读取 `equality_columns` 声明的列，将键值按 schema 类型规范化，再与数据行逐列比较。它作用于所有匹配行，但只考虑 `data_sequence <= delete_sequence` 的数据文件。

示例：`eq_020.jsonl` 序列号 4、等值列 `region`、键 `north`：

- `base_001`（序列号 2）与 `base_002`（序列号 3）的 `north` 行被删除。
- `base_003`（序列号 5）虽然也有 `north` 行，但 5 > 4，不在作用域内，仍可见。
- 键 `nowhere` 没有任何匹配行，产生 `EQUALITY_NO_MATCH`，不改变可见性。

等值列缺失、重复、未声明或不在 schema 中，产生 `AMBIGUOUS_EQUALITY_COLUMNS`；删除文件 schema 哈希不一致产生 `DELETE_SCHEMA_MISMATCH`。这两类文件均不参与可见性计算。原因是无法确认列身份或列语义，继续应用会把“不可信删除”当作事实。

## 3. 序列号相等

判断条件是 `data_sequence <= delete_sequence`，因此相等时**默认应用删除**。例如数据文件序列号 4、等值删除序列号 4，匹配行不可见。位置删除也要求 `delete_sequence >= data_sequence`。该规则由 `test_visibility.test_decision_chain_and_equal_sequence` 固定。

## 4. 过期删除

若删除文件的序列号小于其声明目标数据文件的序列号，删除发生在目标数据版本之前，不能作用于该版本，产生 `STALE_SEQUENCE`，不应用。该规则适用于位置删除和声明了 `target_file_id` 的等值删除。

示例：`pos_011.jsonl` 序列号 2，目标 `base_003` 序列号 5。`base_003` 的 `R0010` 命中位置 0，但删除过期，最终仍可见，reason code 为 `VISIBLE_STALE_DELETE_IGNORED`。未声明目标文件的等值删除没有单一“目标数据文件”，因此不按整文件判过期；它只通过 `data_sequence <= delete_sequence` 限制作用域。

## 5. 重复删除

同一位置在多个条目/文件中重复出现时，第一处结构合法的位置条目保留其正常语义，后续条目产生 `DUPLICATE_POSITION_DELETE` 且不额外参与计算。同一等值删除文件内重复的规范化键产生 `DUPLICATE_EQUALITY_DELETE`，后续键忽略。

示例：`pos_011.jsonl` 的第 1、2 行都删除 `base_003:0`，第 2 行是重复；该文件整体又因过期而不应用。可见性由“过期”决定，但重复事实仍保留在 finding 中。

## 6. 越界与孤儿

`row_pos >= 声明行数` 的位置删除产生 `OUT_OF_RANGE_POSITION`。目标文件不存在、缺少目标或目标不一致产生 `ORPHAN_DELETE_TARGET`。两者都只报告，不创建或推断任何行。

## 7. 冲突优先级

一个删除条目可能有多个问题。所有可独立识别的问题都会写入 findings；用于可见性的 `conflict_code` 按“条目结构非法、目标、越界、重复、模式/作用域、过期”的优先级选一个原因。无论选择哪个 conflict code，只要存在任一冲突，该条目就不应用。

## 8. 为什么有些删除只报告不应用

删除向量是审计对象，不是可信事实。孤儿目标、越界位置、不可用等值列、模式哈希不一致、重复键、过期序列号都说明“这条删除声明无法被可靠地映射到当前数据版本”。直接应用会隐藏错误并污染可见性结果，因此工具保留证据、继续审计，但只让通过全部完整性检查的删除参与计算。

审计始终继续：一个坏删除文件不会中断其他文件、其他行或期望对账。
