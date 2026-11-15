# 行删镜审计报告

## 结论

本次审计未发现期望可见性与引擎计算之间的差异。
共读取 14 行，预计可见 8 行，不可见 6 行；记录 8 条 finding。

## 统计

| 指标 | 值 |
| --- | ---: |
| data_files | 3 |
| delete_files | 4 |
| delete_operations | 10 |
| applicable_deletes | 3 |
| rows | 14 |
| visible_rows | 8 |
| hidden_rows | 6 |
| findings | 8 |
| diffs | 0 |

## 问题分类

| 错误码 | 严重级别 | 数量 |
| --- | --- | ---: |
| AMBIGUOUS_EQUALITY_COLUMNS | warning | 1 |
| DELETE_SCHEMA_MISMATCH | error | 1 |
| DUPLICATE_EQUALITY_DELETE | warning | 1 |
| DUPLICATE_POSITION_DELETE | warning | 1 |
| EQUALITY_NO_MATCH | warning | 1 |
| ORPHAN_DELETE_TARGET | warning | 1 |
| OUT_OF_RANGE_POSITION | warning | 1 |
| STALE_SEQUENCE | warning | 1 |

## 差异样例

未发现差异。

## 可见性摘要

| reason_code | 行数 |
| --- | ---: |
| DELETED_BY_EQUALITY | 4 |
| DELETED_BY_POSITION | 1 |
| DELETED_BY_POSITION_AND_EQUALITY | 1 |
| VISIBLE_NO_DELETE | 7 |
| VISIBLE_STALE_DELETE_IGNORED | 1 |

## 限制说明

- 只支持本项目定义的本地 CSV/JSONL fixture 与 JSON 清单，不读取 Parquet、ORC 或真实湖表。
- 不实现 Iceberg、Delta Lake、Hudi 的协议、快照时间旅行、compaction 或模式演进。
- 不会修改任何数据文件或删除文件；所有建议均为说明性输出。
- 类型系统仅覆盖 string、long、date，完成最小格式校验，不做数据库级类型强制。
- 等值删除只比较规范化后的字段值；缺失字段不参与匹配，空值按显式 null/空串规则处理。
- 报告只包含摘要、finding、差异和决定链摘要，逐行完整链保存在 SQLite 台账。

台账：`out/demo/ledger.sqlite`
