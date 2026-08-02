# dbt-doris Incremental 指南

发布验证、SQL 次数判定和失败注入清单见
[Incremental 测试方案](incremental-test-plan.zh-CN.md)。

dbt-doris 内置支持三种 Incremental 策略：`append`、`merge` 和
`insert_overwrite`。已有目标表且 `on_schema_change='ignore'` 时，这三种
策略都只执行一条最终 DML，不会先把同一批数据写入物理临时表。

## 正式版本验证

最终 CTAS Snapshot + Durable Marker + Pre-model Ordering 实现已完成正式矩阵：

| Doris | FE/BE 完整 Version | 完整 Functional | 聚焦 Incremental | 状态 |
| --- | --- | --- | --- | --- |
| 2.1.11 | `doris-2.1.11-rc01-97b77e6cda` | 88 passed / 106 warnings / 96.64s | 26 passed / 27 warnings / 21.49s | passed |
| 3.0.8 | `doris-3.0.8-rc01-09b0cc49a6` | 88 passed / 106 warnings / 96.59s | 26 passed / 27 warnings / 21.79s | passed |
| 3.1.4 | `doris-3.1.4-rc02-7f5ba43de6` | 88 passed / 106 warnings / 99.73s | 26 passed / 27 warnings / 22.46s | passed |
| 4.0.7 | `doris-4.0.7-rc02-35854e7e92a` | 88 passed / 106 warnings / 99.05s | 26 passed / 27 warnings / 22.26s | passed |
| 4.1.3 | `doris-4.1.3-rc02-7126cf65d96` | 88 passed / 106 warnings / 99.16s | 26 passed / 27 warnings / 22.79s | passed |

这里的 `passed` 仅表示上表精确版本通过已登记的 88 项完整 Functional、26 项
聚焦 Incremental、版本身份和清理检查；测试方案中的 INC-001、INC-002、
INC-053、INC-063、INC-069、INC-071 仍是待补增强项，不能把本表解读为所有
规划场景均已自动化。

所有版本的 FE/BE 完整 Version 均一致且 `Alive=true`，测试数据库与 Helper
Relation 残留均为 0。验证环境为 dbt Core 1.12.0、Adapter 1.0.0、Python
3.12.13；正式 Adapter SHA 为 `259b14e0ff77c1dac4c1963b918e0612b2901358`、
`dirty=false`。每份版本 JSON 的 `doris_version_gate` 均记录对应
`expected_release`、上表完整 `reported_build` 与 `status=passed`。Unit 为
324 passed / 9 warnings / 26.71s，Flake8 和 diff check 通过。旧 dirty 工作树
运行只作预验证和历史记录，不是正式证据。
2.1.11 暴露的调用 Session `sql_mode` 问题已通过 Pre-model Ordering 修复，并由
该版本的聚焦与完整运行验证。干净 Package 输出位于
`/tmp/dbt-doris-package-clean.tUhMxp`：75,660-byte wheel SHA-256 为
`edcbc1bae94e440c7be25f71ec96b6c91e4a5e71af29604561f4d99264584725`，
119,127-byte sdist 为
`ffe4c9c41e8a7f6a24fb43935ec30535748095b2a807b634fe2266ede0b43ef9`，
Twine 7.0.0 双 PASSED。Python 3.12.13 全新 venv
`/tmp/dbt-doris-wheel-clean-py312.lPTWhm` 的 wheel 安装、`site-packages`
导入、三个 Macro、策略列表与 `pip check` 均通过。

## 策略选择

| 配置 | Doris 目标表 | 普通增量语句 | 结果语义 |
| --- | --- | --- | --- |
| `append` | Duplicate Key | `INSERT INTO` | 追加本批全部行 |
| `merge` | MOW 或 MOR Unique Key | `INSERT INTO` | 按 Unique Key 完整行 Upsert |
| `insert_overwrite` | 可写 Doris 表 | `INSERT OVERWRITE` | 覆盖整表或指定分区 |

没有显式配置 `incremental_strategy` 时：

- 配置了 `unique_key`：使用 `merge`；
- 没有 `unique_key`：使用 `append`。

`delete+insert` 和 `delete_insert` 均不受支持。需要按 Key 更新或插入时使用
`merge`。

## `append`

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    duplicate_key=['id'],
    distributed_by=['id']
) }}

select id, value from source_table
```

首次运行创建 Duplicate Key 目标表；后续运行通过一条 `INSERT INTO` 追加。

## `merge`

```sql
{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['tenant_id', 'id'],
    distributed_by=['tenant_id']
) }}

select tenant_id, id, value, updated_at from source_table
```

这里的 `merge` 是 dbt 的结果语义，不代表当前会生成 Doris 原生
`MERGE INTO`。Adapter 向 Unique Key 表执行完整行 `INSERT INTO`，由 Doris
存储模型完成 Upsert：

- 同时支持 Merge-on-Write（MOW）和 Merge-on-Read（MOR）Unique Key 表；
- 新建 Unique Key 表默认使用 MOW；显式配置
  `properties={'enable_unique_key_merge_on_write': 'false'}` 可创建 MOR；
- 模型 SQL 中 Key 列不必写在最前面；首次创建和 Full Refresh 会按
  `unique_key` 配置顺序将 Key 投影为 Doris 物理 Schema 的前缀；
- `function_column.sequence_col` 使用模型返回的可见列，并继续由 Doris 按
  Sequence 规则决定新旧版本；
- 每一批 Source 中，同一个单列或复合 `unique_key` 只能出现一次。Adapter
  在同一条 Upsert 语句内校验重复 Key；失败时不会先改写目标表。内部校验列从
  n+1 个保留候选中选择不与本批 n 个目标列重名的 Alias，因此用户列名不会与
  Merge Guard 冲突。

`merge_update_columns`、`merge_exclude_columns` 和
`incremental_predicates` 暂不支持，因为它们需要条件或局部列更新。未出现在
一次 `INSERT` 列清单中的目标列遵循 Doris 对省略列的默认值或 `NULL` 规则。

裸 `sequence_col` 不是 dbt-doris 配置。请使用 Doris 表属性，例如：

```python
properties={
    'function_column.sequence_col': 'updated_at'
}
```

`function_column.sequence_type` 依赖写入 Doris 隐藏列
`__DORIS_SEQUENCE_COL__`，当前 Incremental 列映射不暴露该隐藏列，因此会在
执行前明确拒绝；请改用 `function_column.sequence_col`。

## `insert_overwrite`

`insert_overwrite` 不能同时配置 `unique_key`。这是迁移保护：旧版曾把该组合
当成 Unique Key Upsert；新版若直接接受为原生覆盖，可能静默删除本批未出现的
旧行。需要 Upsert 时改用 `merge`；确实需要覆盖时删除 `unique_key`，显式选择
以下原生语义。

不配置 `overwrite_partitions` 时覆盖整表：

```sql
INSERT OVERWRITE TABLE target (...)
SELECT ...;
```

静态覆盖指定分区：

```python
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    partition_by=['event_date'],
    overwrite_partitions=['p20260801', 'p20260802']
) }}
```

动态覆盖本批数据涉及的分区：

```python
{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    partition_by=['event_date'],
    overwrite_partitions='*'
) }}
```

`overwrite_partitions` 只能与 `insert_overwrite` 和分区表一起使用。`'*'`
不能与静态分区名混用。

## 临时关系与 Full Refresh

已有目标表的普通 `on_schema_change='ignore'` 增量运行会创建名为
`__dbt_tmp` 的普通逻辑 View。这个 View 只保存模型 SQL 定义，用于读取列名、
类型和字符串长度；它不保存查询结果，也不会造成一次数据物化。随后策略读取
该 View，向目标表执行唯一一条 DML，运行结束后删除 View。首次运行则直接
CTAS 创建目标表。

以下场景会创建物理关系：

- `on_schema_change` 不是 `ignore`：使用物理 staging table 冻结本批数据，
  避免修改目标 Schema 前后读取到不同批次；
- 自定义 Incremental 策略：使用物理 staging 保持 dbt 的标准策略参数契约；
- Full Refresh：先创建 intermediate table，再用 Doris 元数据交换安全替换
  目标表。数据只写入 intermediate 一次，不会再写最终表一次。

另有一类与“冻结增量批次”不同的物理例外：Canonical View →
Table/MV/Partition 的正向类型切换使用专用 CTAS Snapshot：

```sql
CREATE TABLE backup
DISTRIBUTED BY RANDOM BUCKETS AUTO
PROPERTIES (
  "enable_duplicate_without_keys_by_default" = "true",
  "replication_num" = "..."
)
AS SELECT * FROM source_view;
```

Adapter 绝不重放 View DDL，也不假设 View 保留创建时的 SQL Mode/Session 语义。
Doris 2.1.11 实测表明，查询旧 View 的结果可能受调用 Session 当前 `sql_mode`
影响。因此 Snapshot 必须在新模型任何 Pre-hook、`sql_header` 或 DDL 之前执行，
使用尚未被新模型改变的 Pre-model Session。Snapshot 固定 RANDOM/AUTO 分桶和
`enable_duplicate_without_keys_by_default=true`；仅允许从当前模型配置携带
`replication_num` 或 `replication_allocation`，绝不从旧 View 推断副本属性，也不
继承新模型的 Key、Distribution、Partition、Contract 或 `sql_header`。这避免把
DOUBLE 等不可作 Key/Hash 的首列误选为物理 Key 或分桶列。该正向 Snapshot 是
物理 Table。Snapshot Helper 在源/目标同名或目标已存在时，会在执行任何 SQL 前
失败。
Generic View Rename/Exchange 不提供模拟语义，直接拒绝。

Snapshot 保存的是当时从旧 View 可查询的结果数据，不保存 View Definition、创建
时 Session 状态、Comment、Grant 或完全一致的 Schema 属性。

CTAS 失败时，Canonical 旧 View 继续在线，且新模型 Hook、Header、DDL 均未执行。
CTAS 成功后也不会立即删除旧 View：Adapter 先运行新模型上下文并完成 Replacement
构建，期间 Canonical 名仍指向旧 View；Replacement 就绪后才 Drop 旧 View 并将
Replacement Rename 为 Canonical。Snapshot Marker 保留到整个生命周期成功后才
清理。SQL Mode 用例必须断言 Pre-model Session 当时实际查询到的数据以及上述
Ordering，不能再用 View 的创建模式推导查询结果。

这类 Snapshot 仅用于正向类型切换，不改变普通 `append`、`merge`、
`insert_overwrite` 的逻辑临时 View + 一条最终 DML 契约。

Doris 执行 `INSERT OVERWRITE` 时内部使用的临时分区属于数据库实现细节，
不等同于 dbt-doris 的物理 staging table。

`__dbt_backup` 的恢复边界与正向 CTAS 不同。Incremental/Partition 发现 Canonical
缺失而 Backup 存在时，不会先把 Backup 恢复到 Canonical，也不会执行、Snapshot、
Rename 或提前删除它。Backup 保持原名作为 Durable Marker，可以是 Legacy View、
Table 或 Async MV；Legacy View Backup 因此完全不走 CTAS。

本轮直接从 Model SQL 完整构建 Canonical。因为 Canonical 在编译时仍不存在，
`is_incremental()` 为 false；如果本轮再次失败，Canonical 继续缺失，下一轮仍走
完整构建分支。旧数据只在 `__dbt_backup` 名下可查询，Adapter 不保证失败期间
Canonical 名可用。只有 Main Build、Index、Grants、Docs、Hook 和 Commit 等完整
生命周期全部成功后，才删除 Durable Marker。Incremental 与 Partition 的三轮
Functional 用例都覆盖“保留 Marker → 再次失败 → 成功构建后清理”的流程。

若失败后 Canonical 旧 View 仍在线，遗留的物理 Snapshot 只是上一次尝试的 Marker；
下一次运行在重新冻结旧 View 前先清理或替换该 Marker。若失败发生在 Drop View 与
Rename Replacement 的切换窗口，使 Canonical 缺失，则物理 Marker 是唯一旧数据
副本。对本文讨论的 Incremental/Partition，必须按上一段 Durable Marker 规则
保留，直到 Canonical 完整重建成功；Table/MV Materialization 不使用该
No-restore 规则，而是先恢复 Canonical 再重试。

## 从旧实现迁移

旧版 dbt-doris 把显式 `incremental_strategy='insert_overwrite'` 与
`unique_key` 的组合实现成 Unique Key `INSERT INTO` Upsert。新版不会静默
改变该配置的结果，而是在 Hook 和写入前拒绝并提示迁移：

- 需要保留旧 Upsert 语义：改用 `incremental_strategy='merge'` 并配置
  `unique_key`；
- 确实需要整表或分区替换：继续使用 `insert_overwrite`，但删除
  `unique_key`，明确选择覆盖范围内缺失行会被删除的语义；
- 旧的 `delete+insert` 模型：改用 Unique Key 目标和 `merge`，并通过一次
  Full Refresh 重建不兼容的现有目标表。
