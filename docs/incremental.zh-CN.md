# dbt-doris Incremental 指南

发布验证、SQL 次数判定和失败注入清单见
[Incremental 测试方案](incremental-test-plan.zh-CN.md)。

dbt-doris 内置支持三种 Incremental 策略：`append`、`merge` 和
`insert_overwrite`。已有目标表且 `on_schema_change='ignore'` 时，这三种
策略都只执行一条最终 DML，不会先把同一批数据写入物理临时表。

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
  在同一条 Upsert 语句内校验重复 Key；失败时不会先改写目标表。

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

Doris 执行 `INSERT OVERWRITE` 时内部使用的临时分区属于数据库实现细节，
不等同于 dbt-doris 的物理 staging table。

每次运行开始时，Adapter 会先清理上次失败可能留下的同名临时关系；
View 转 Table 失败时则先恢复唯一的备份对象，再做清理。因此重试不会读取
上一次未完成的 staging 批次。

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
