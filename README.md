# 行删镜

**本地湖表的行级删除可见性核验与 Merge-on-Read 审计工具。**

行删镜读取轻量本地湖表 fixture（表模式、快照、CSV/JSONL 数据文件、位置/等值删除文件、期望可见结果），按声明的删除语义计算每一行是否可见，检查删除向量自身的孤儿、越界、重复、过期、无匹配、校验失败与模式不一致问题，并输出可复现的 JSON / Markdown / HTML 报告、SQLite 台账和逐行解释链。

## 边界

**是**：纯本地、离线、确定性可复现的删除可见性核验器；配置驱动、fixture 可替换；带 SQLite 台账、幂等重跑、差异对账、golden 回归。

**不是**：不是 Iceberg / Delta Lake / Hudi 读写实现；不是 SQL 查询引擎；不是修复或湖表维护工具；不做时间旅行、compaction、冷热分层、模式演进、分区裁剪、水位审计、SCD2、血缘或调度。

它只回答：**在这个本地快照里，哪些行被正确删除、哪些行仍应可见、删除向量是否可信、某行为什么最终可见或不可见。**

## 快速上手

```bash
python -m lakedel doctor --workspace examples/lake
python -m lakedel audit --workspace examples/lake --out out/audit --format json,md,html
python -m lakedel explain --workspace examples/lake --row-id R0004
python -m lakedel explain --workspace examples/lake --file-id base_001 --row-pos 1
python -m lakedel verify-ledger --db out/audit/ledger.sqlite
python -m lakedel demo
python -m unittest discover -s tests -v
```

## 删除后质检抽样证据样例

隔离的 `examples/quality_transfer` 包含同一批 40 行的删除前后审计快照。`draw.json` 固定删除前在 east、west 各无放回抽 10 行，并给出人工质检结果；删除后，位置删除文件隐藏 east 的 5 条未抽中行。只读入口核对两次审计、样本身份、删除策略和剩余可见总体，并按两份事前合同决定当前可见总体（east 15、west 20，共 35 行）是否取得质量发布证书：

```bash
PYTHONPATH=src python -B -m lakedel.cohort_transfer --root examples/quality_transfer --draw examples/quality_transfer/draw.json
PYTHONPATH=src python -B -m lakedel.cohort_transfer --root examples/quality_transfer --draw examples/quality_transfer/draw.json --quality-threshold 0.2
```

两份合同的质量门槛均为当前可见总体缺陷率单侧 95% 上界不超过 25%（可用 `--quality-threshold` 收紧）。`unconditional_portable` 把删除前固定随机设计的无条件覆盖保证保守携带到只删不增的 after 总体：每个原 region 分配 α=0.025（整体至少 95% 同时覆盖），按有限总体无放回超几何精确反演各层缺陷数上界（零缺陷样本下 east、west 各 4 条），仅在有收据确认已删缺陷时扣减；合成上界 8/35≈22.86% 通过 25% 门槛，签发绑定 before/after `run_key`、当前总体与逐层 N/n/x 及尾概率见证的证书（收紧到 20% 则阻断）。`fresh_current_draw` 要求当前可见集合形成后再随机抽样；fixture 的 20 条收据都发生在删除之前，即使全部幸存也不满足该证据时点承诺，故暂缓并指出需重抽，而不是误报为质量不达标。任何新增行、幸存行内容变化、审计 error/diff、抽样身份或删除声明失配都会拒绝签发并给出证据原因；相同输入重放输出字节稳定。

## 删除请求履约样例

隔离的 `examples/request_lake` 以 `_row_id` 声明删除请求，并提供三个按序快照。新增只读命令按快照顺序重算可见性，报告请求在各观察时点的待履约、按期履约、逾期履约、违约、再出现或证据不确定状态，不修改湖文件：

```bash
python -m lakedel request-status --workspace examples/request_lake --through snap_002
python -m lakedel request-status --workspace examples/request_lake
```

前者给出 `req-001` 按期履约；后者在 `snap_003` 发现同一行再次可见，报告 `regressed`。请求声明位于 `requests/erasure_requests.json`，包含目标行身份、受理时间和履约期限。这个流程只核验本地快照中的请求状态，不发送通知或实际删除数据。

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

完整规则与例子见 `docs/DELETE_SEMANTICS.md`。

## 报告与台账

- `report.json`：项目/规则版本、快照摘要、统计、finding、差异明细、可见性摘要、台账路径。键排序稳定。
- `report.md`：结论、统计表、问题分类表、差异样例、可见性摘要、限制说明。
- `report.html`：无 CDN、无外部字体/脚本、样式内联的自包含页面。
- `ledger.sqlite`：`runs`、`artifacts`、`findings`、`row_visibility`、`diff_summary`。`run_key` 由 workspace 指纹、快照 ID 与规则版本生成，相同输入重跑不会重复插入 run。
- `explain` 输出该行的初始可见性、位置/等值命中、过期忽略项、最终可见性与 reason code。

## 退出码

| 码 | 含义 |
| ---: | --- |
| 0 | 成功，且没有期望可见性差异 |
| 2 | 审计完成，但存在期望差异 |
| 3 | 输入、路径或台账错误 |
| 4 | 内部错误 |

finding 是审计观察；只有 fixture 对账差异会改变 `audit` 的退出码。台账校验失败返回 `3`。

## 已知限制

不读取 Parquet/ORC 或真实湖表；不修改任何原始文件；类型系统只覆盖 `string`、`long`、`date`；等值删除只做规范化字段相等比较；不处理删除文件合并、快照过期或 schema 演进。详细字段与错误码见 `docs/SPEC.md`。

## 合规与离线声明

运行期仅使用 Python 3.10+ 标准库，不访问网络、云服务、对象存储、消息队列或第三方 API，不发起 DNS，不监听端口。仓库内主体、ID、路径、字段值均为合成数据，不含真实密钥、令牌、公司、域名、IP 或人员信息。测试使用 `unittest`。
