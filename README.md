# 行删镜

**本地湖表的行级删除可见性核验与 Merge-on-Read 审计工具。**

行删镜读取轻量本地湖表 fixture（表模式、快照、CSV/JSONL 数据文件、位置/等值删除文件、期望可见结果），按声明的删除语义计算每一行是否可见，检查删除向量自身的孤儿、越界、重复、过期、无匹配、校验失败与模式不一致问题，并输出可复现的 JSON / Markdown / HTML 报告、SQLite 台账和逐行解释链。

## 边界

**是**：纯本地、离线、确定性可复现的删除可见性核验器；配置驱动、fixture 可替换；带 SQLite 台账、幂等重跑、差异对账、golden 回归。

**不是**：不是 Iceberg / Delta Lake / Hudi 读写实现；不是 SQL 查询引擎；不是修复或湖表维护工具；不做时间旅行、compaction、冷热分层、模式演进、分区裁剪、水位审计、SCD2、血缘或调度。

它只回答：**在这个本地快照里，哪些行被正确删除、哪些行仍应可见、删除向量是否可信、某行为什么最终可见或不可见。** 当输入文件哈希与清单不符时，它还会回答：**哪些结论有可信证据、哪些只能待核定。**

## 快速上手

```bash
python -m lakedel doctor --workspace examples/lake
python -m lakedel audit --workspace examples/lake --out out/audit --format json,md,html
python -m lakedel audit --workspace examples/lake --out out/probe --view investigate
python -m lakedel explain --workspace examples/lake --row-id R0004
python -m lakedel explain --workspace examples/lake --file-id base_001 --row-pos 1
python -m lakedel verify-ledger --db out/audit/ledger.sqlite
python -m lakedel demo
python -m unittest discover -s tests -v
```

`demo` 在 `out/demo/` 生成 `report.json`、`report.md`、`report.html` 和 `ledger.sqlite`。示例包含 8 条删除向量 finding，但期望结果与语义计算结果一致，因此退出码为 `0`。存在期望差异时 `audit` 返回 `2`。

## Fixture 结构

```text
examples/lake/
  table.json                 # 表 ID、规则版本、期望结果路径、列模式
  snapshots/snap_001.json    # 快照 ID、序列号、数据/删除文件清单与哈希
  data/base_001.csv          # CSV 数据文件，含 _row_id 与 _row_pos
  data/base_002.csv
  data/base_003.jsonl        # JSONL 数据文件
  deletes/pos_010.jsonl      # 有效位置删除
  deletes/pos_011.jsonl      # 过期、重复、越界、孤儿目标
  deletes/eq_020.jsonl       # 重复等值键、无匹配等值键
  deletes/eq_021.jsonl       # 模式哈希不一致、未声明等值列
  expected/visible_snap_001.csv  # 每行的期望可见布尔值
```

所有清单路径都是 workspace 内的 POSIX 相对路径，禁止绝对路径、`..`、反斜杠和符号链接逃逸。

## 删除语义摘要

规则版本：`MOR-1.0`，与 `table.json` 的 `rule_version` 一致。

- 位置删除只作用于清单 `target_file_id`（或条目 `file_id`）与 `row_pos` 指定的行。
- 等值删除作用于所有满足键值的数据行，但只处理 `data_sequence <= delete_sequence` 的数据文件；序列号相等时默认应用删除。
- 删除序列号小于目标数据文件序列号的位置删除（以及带目标的等值删除）是过期删除，只报告 `STALE_SEQUENCE`，不应用。
- 孤儿目标、越界位置、重复位置/等值键、无匹配键、模式哈希不一致、等值列不可用的删除保留在审计记录中，不参与可见性计算。重复位置删除的第一条合法条目仍可生效，后续重复条目被忽略。
- 等值删除键按 schema 类型规范化后比较；删除文件的 schema 哈希不一致时整文件不应用，防止错误列语义污染结果。

### 完整性分层

数据文件或删除文件的实际 SHA 与清单不符时，工具不修复文件、不修改输入，而是把内容降级为**调查证据**：

- 每行给出 `certain_visible`、`certain_hidden`、`indeterminate` 三态。可信位置/等值删除的任一有效命中足以证明 `certain_hidden`；可信数据行存在且所有可能适用的可信与不可信删除都被排除才是 `certain_visible`；其余为 `indeterminate`。
- 数据文件不可信时，解析出的 `_row_id` 与等值键不能作为确定身份，该行连同其上删除命中一起降级。
- 同一受损删除文件涉及的多行共享来源证据（`untrusted_sources` / `untrusted_equality_rows`），不会被当作每行独立可控的可靠删除。
- 暂定布尔结果仍按当前可解析内容计算，只在调查视图中展示。

两种明确授权用途：

| 用途 | 命令 | 结论 |
| --- | --- | --- |
| 发布核验 | `audit`（默认 `--view publish`） | 仅当每个 expected 行的确定态与期望一致且无 `indeterminate` 时才 `publishable=true`；否则退出码 2 |
| 调查视图 | `audit --view investigate`、`explain` | 允许列出暂定布尔结果，同时显示可信区间与来源，永远不标记为可发布 |

完整规则与例子见 `docs/DELETE_SEMANTICS.md`、`docs/INTEGRITY.md`。

## 报告与台账

- `report.json`：项目/规则版本、快照摘要、统计、finding、差异明细、可见性摘要、台账路径。键排序稳定。
- `report.md`：结论、统计表、问题分类表、差异样例、可见性摘要、限制说明。
- `report.html`：无 CDN、无外部字体/脚本、样式内联的自包含页面。
- `ledger.sqlite`：`runs`、`artifacts`、`findings`、`row_visibility`（含三态、可信/可疑命中、可信区间）、`source_integrity`、`outcome`、`diff_summary`。`run_key` 由 workspace 指纹、快照 ID 与规则版本生成，相同输入重跑不会重复插入 run。
- `explain` 输出该行的初始可见性、位置/等值命中、过期忽略项、最终可见性与 reason code。

## 退出码

| 码 | 含义 |
| ---: | --- |
| 0 | 成功；发布核验可发布，或调查视图已完成 |
| 2 | 发布核验未通过（期望差异或存在待核定行） |
| 3 | 输入、路径或台账错误 |
| 4 | 内部错误 |

finding 是审计观察；发布核验下，fixture 对账差异或 `indeterminate` 行都会使 `audit` 返回 `2`。`--view investigate` 只用于调查展示，暂定结果不触发发布级退出码。台账校验失败返回 `3`。

## 已知限制

不读取 Parquet/ORC 或真实湖表；不修改任何原始文件；类型系统只覆盖 `string`、`long`、`date`；等值删除只做规范化字段相等比较；不处理删除文件合并、快照过期或 schema 演进。详细字段与错误码见 `docs/SPEC.md`。

## 合规与离线声明

运行期仅使用 Python 3.10+ 标准库，不访问网络、云服务、对象存储、消息队列或第三方 API，不发起 DNS，不监听端口。仓库内主体、ID、路径、字段值均为合成数据，不含真实密钥、令牌、公司、域名、IP 或人员信息。测试使用 `unittest`。
