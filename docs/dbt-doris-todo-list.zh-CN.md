# dbt-doris TODO

## 已确定的方案

- 只以 **dbt Core 1.12.x** 为开发和测试基线，Python 使用 3.10+。
- 当前只开发 Python dbt Adapter，不同时开发 Fusion Adapter。
- 覆盖 dbt Core 1.12 官方五种 Model Materialization。
- Incremental 对齐 dbt Core 1.12 的五种内置策略：

| 策略 | Doris 实现 | 阶段 |
| --- | --- | --- |
| `append` | `INSERT INTO` 追加 | P0 |
| `merge` | Unique Key 表 + `INSERT INTO` Upsert | P0 |
| `delete+insert` | 按 `unique_key` 删除后重新插入 | P0 |
| `insert_overwrite` | Doris 原生 `INSERT OVERWRITE` | P0 |
| `microbatch` | 按 `event_time` 拆分时间批次 | P1 |

当前名为 `insert_overwrite` 的实现，实际上是向 Unique Key 表执行
`INSERT INTO`。同 Key 行更新、新 Key 行插入、未出现的旧 Key 保留，因此它应
归到 `merge`，而不是 `insert_overwrite`。

这里的 `merge` 指结果语义，不要求 SQL 文本必须是 `MERGE INTO`。首版复用
Doris Unique Key Upsert；以后需要局部列更新等能力时，再评估原生
`MERGE INTO`。

## P0：升级到 dbt Core 1.12

- [ ] 将依赖、Adapter 版本和测试环境统一到 dbt Core 1.12.x。
- [ ] 更新已变化的 Adapter API 和宏接口。
- [ ] 验证源码安装、wheel 构建、wheel 安装和 `pip check`。
- [ ] CI 运行 Unit Test 和真实 Doris Functional Test。
- [ ] Functional Test 至少覆盖 `dbt debug/seed/run/test/snapshot`。

## P0：覆盖官方 Model Materialization

| Materialization | 当前状态 | 下一步 |
| --- | --- | --- |
| `view` | 已实现 | 验证 dbt 1.12 生命周期、Docs、Grants 和对象类型切换 |
| `table` | 已实现 | 完善安全替换、Contracts 和 Doris Table 配置 |
| `incremental` | 部分实现 | 完善官方策略，见下一节 |
| `ephemeral` | dbt Core 提供 | 验证 CTE 编译和 `ref()`，不新增 Doris DDL |
| `materialized_view` | 已实现 | Doris Async Materialized View 生命周期已闭环；Sync MV 不在范围内 |

- [x] 测试 `view`、`table` 和 `materialized_view` 之间的
  Relation 类型切换。
- [ ] 补 Incremental 与其他 Materialization 的 Relation 类型切换覆盖。
- [ ] 将现有自定义 `partition` Materialization 的能力并入
  `incremental_strategy='insert_overwrite'`，保留兼容迁移说明。

Snapshot、Seed 和 Data Test 是独立 dbt Resource，不属于 Model
Materialization；其 Doris 兼容工作放在 P1。

## P0：完善 Incremental 基础策略

- [ ] 接入 dbt 1.12 标准 Incremental Strategy Dispatch 和对应策略宏。
- [ ] 未支持的策略或配置在执行 SQL 前明确报错。
- [ ] 完善策略与 Doris 表模型的映射：
  - `append` 使用 Duplicate Key；
  - `merge` 使用 Merge-on-Write Unique Key；
  - `delete+insert` 和 `insert_overwrite` 校验目标表模型是否兼容。
- [ ] 首次建表、普通增量和 Full Refresh 共用 Duplicate/Unique Key DDL，
  并保留 Key、Partition、Distribution 和 Properties。

### `append`

- [ ] 保留当前 Duplicate Key 表 + `INSERT INTO` 实现。
- [ ] 测试首次创建、重复运行和 Full Refresh。

### `merge`

- [ ] 将当前错误命名的 `insert_overwrite` 路径改为 `merge`。
- [ ] 要求配置 `unique_key`，支持单列和复合 Key。
- [ ] 首次运行创建 Merge-on-Write Unique Key 目标表。
- [ ] 后续运行继续使用 `INSERT INTO`，利用 Unique Key 完成 Upsert。
- [ ] 校验现有目标表的 Key 类型和 Key 列；不兼容时提示
  `--full-refresh`。
- [ ] 测试更新已有行、插入新行、保留本批未出现的旧行。

### `delete+insert`

- [ ] 要求配置 `unique_key`，支持单列和复合 Key。
- [ ] 使用临时表确定本批 Key，先删除目标表中的匹配行，再插入本批结果。
- [ ] 测试已有 Key 替换、新 Key 插入、未匹配旧行保留和失败恢复。

### `insert_overwrite`

- [ ] 不再要求 `unique_key`。
- [ ] 首先实现整表覆盖：

```sql
INSERT OVERWRITE TABLE target
SELECT ...;
```

- [ ] 再实现指定分区覆盖：

```sql
INSERT OVERWRITE TABLE target PARTITION (p1, p2)
SELECT ...;
```

- [ ] 测试覆盖范围内旧数据被删除、非覆盖分区保持不变。
- [ ] 明确失败清理和重试行为。

### 兼容和迁移

- [ ] 旧项目若想按 Key Upsert，将
  `incremental_strategy='insert_overwrite'` 改为 `merge`。
- [ ] 旧项目若想覆盖整表或分区，继续使用 `insert_overwrite`。
- [ ] 在 Release Note 中明确这是行为修正：新的 `insert_overwrite` 会删除
  覆盖范围内未出现在本批的数据。

## P0：实现 Materialized View

实现独立的 `materialized='materialized_view'`，对应 Doris Async Materialized
View，不作为 Incremental Strategy。

- [x] 根据 Model SQL 生成 `CREATE MATERIALIZED VIEW ... AS ...`。入口：
  `materialized='materialized_view'`，支持 `ref()`、`source()`、Alias 和目标
  Schema。
- [x] 支持 `BUILD IMMEDIATE/DEFERRED`。入口：`build_mode`；Immediate 默认
  等待首次构建任务完成，Deferred 不发起首次构建。
- [x] 支持 `REFRESH AUTO/COMPLETE` 和
  `ON MANUAL/SCHEDULE/COMMIT`。入口：`refresh_method`、
  `refresh_trigger`、`refresh_schedule`；生产 Schedule Unit 为
  minute/hour/day/week，Adapter 拒绝测试专用的 second。
- [x] 支持刷新周期、`PARTITION BY`、Distribution、Buckets 和 Properties。
  入口：`refresh_schedule`、`partition_by`、`distribution_type`、
  `distributed_by`、`buckets`、`properties` 和 `replication_num`。
- [x] 正确识别、删除和重建 Materialized View Relation。入口：
  `mv_infos` Relation 补全、MV 专用 Drop/Rename，以及 Table/View/MV
  类型切换。
- [x] 重复执行 `dbt run` 时保持幂等；配置变化时明确更新或重建。入口：
  归一化定义 Hash、`on_configuration_change`、临时 MV 和 Doris 原子
  `REPLACE WITH MATERIALIZED VIEW`；Pre/Post Hook 与未完成部署恢复已覆盖，
  包括 Replace 后 Post-hook 失败时先原子回滚旧 MV 再重试。
- [x] 支持 `ON MANUAL` Refresh 并返回 Doris Task 状态。首次 Create/Replace
  只按 `BUILD IMMEDIATE` 等首次 Task，不额外 Refresh；定义未变时，每次选中
  Manual Model 都提交 `REFRESH MATERIALIZED VIEW ... AUTO/COMPLETE` 并默认
  等待，关闭等待时仍提交但不轮询；Schedule/Commit 未变时 Skip。
  刷新分流只由 `refresh_trigger` 决定，不提供 `refresh_on_run`。
  `BUILD DEFERRED + MANUAL` 第一次只创建、第二次运行刷新。入口：
  `wait_for_refresh`、
  `refresh_wait_timeout`、`refresh_poll_interval`；不提供指定分区刷新。
- [x] Functional Test 覆盖创建、查询、Manual Refresh、Schedule/Commit Skip、
  Deferred 第二次运行、Task 等待/只提交、配置变化和删除。入口：
  `test/functional/adapter/test_doris_materialized_view.py` 和
  `test_doris_materialized_view_basic.py`、
  `test_doris_materialized_view_complete.py` 和
  `test_doris_grants.py`；Unit Test 同时覆盖 DDL、配置校验、Docs、Grants、
  Hook、状态轮询和失败恢复。

附加完成项：

- `persist_docs.relation/columns` 已覆盖 MV Relation/Column Comment，且仅在启用时
  纳入定义 Hash。
- Doris 专用 Grants 支持显式 `role:<name>`、
  `user:<name>@<host>` Principal，以及 `grants_mode=replace/additive`。
- 真实 Doris 集群 Functional E2E 当前只覆盖
  4.1.2-rc01（`doris-4.1.2-rc01-4536b29f712`）。当前运行时 Gate 接受
  2.x 中不低于 2.1.5 的版本、除 3.0.0 外的 3.x，以及主版本 4 及以上；
  模拟版本字符串的 Gate 单测覆盖 2.1.5、2.1.10、3.0.1、3.1.0 和 4.1.2，
  但不验证 Doris 功能兼容性。除已实测版本外，投入生产前需要在对应版本上
  运行 Functional Test。运行时 `SHOW FRONTENDS` 优先校验当前连接 FE 和
  Master FE，无法识别角色时退回首行。
- Sync Materialized View（Rollup）保持独立评估，本 TODO 不包含该能力。

## P1：完善 Incremental 高级能力

- [ ] `microbatch`：支持 `event_time`、`begin`、`batch_size`、`lookback`
  和并行批次。
- [ ] `on_schema_change`：支持 `ignore`、`fail`、
  `append_new_columns`、`sync_all_columns`。
- [ ] Merge 配置：支持 `merge_update_columns`、
  `merge_exclude_columns` 和 `incremental_predicates`；无法支持的组合明确报错。

## P1：补齐 dbt 通用能力

| 能力 | 大概功能 | 用户入口 |
| --- | --- | --- |
| Snapshot | 用 Check/Timestamp Strategy 保存数据历史版本，保证失败时旧历史仍可用 | `dbt snapshot` |
| Contracts | 建表前校验 Model 输出的列名和类型是否符合 YAML 声明 | `contract.enforced: true` |
| Persist Docs | 把 Model 和 Column Description 写入 Doris Comment | `persist_docs` |
| Source Freshness | 按源表最近加载时间产生 Pass、Warn 或 Error | `dbt source freshness` |
| Store Failures | 把 Data Test 失败的具体数据行保存到审计表 | `dbt test --store-failures` |
| Grants | 按 Model Config 授权并回收 Relation 的过期权限 | `grants:` |

- [ ] 接入适用的 `dbt-tests-adapter` 官方测试，作为上述能力的兼容性验收。

## P2：完善 Doris Table 原生能力

- [ ] 在 P0 已有的 Duplicate/Unique Key 支持上，抽取供 Table、Incremental
  和 Full Refresh 共用的配置与 DDL 层。
- [ ] 新增 Aggregate Key、聚合函数配置及测试；只开放能够保证正确结果的
  Incremental 策略，不默认套用 `append` 或 `merge`。
- [ ] RANGE/LIST/Auto/Dynamic Partition。
- [ ] HASH/RANDOM Distribution 和 `BUCKETS AUTO`。
- [ ] Inverted、Bloom Filter、Bitmap 等索引。

## P3：生产能力

- [ ] SSL、Timeout、Retry 和多 FE Failover。
- [ ] Query ID、Invocation ID、影响行数和执行耗时。
- [ ] Doris 服务端 Query Cancel。
- [ ] External Catalog 元数据支持和性能优化。
- [ ] 自动构建、测试和发布 wheel。
