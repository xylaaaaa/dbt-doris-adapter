
# 任务 4：Apache Doris #65967 dbt-doris 支持异步物化视图需求说明

## 文档信息

| 项目 | 内容 |
| --- | --- |
| 任务编号 | 4 |
| 对应任务 | [Apache Doris #65967](https://github.com/apache/doris/issues/65967) |
| Issue 标题 | `[Feature] (dbt-doris) Support async materialized views as a dbt materialization` |
| Issue 状态 | Open |
| Issue 类型 | `kind/feature` |
| 创建时间 | 2026-07-23 |
| 文档日期 | 2026-07-27 |
| 文档目的 | 把 Issue 中的一句话需求展开成可讨论、可开发、可验收的需求 |
| 实现状态 | Async Materialized View 已按本文验收项完成；Sync MV 不在范围内 |

## 0. 一句话结论

这个任务要让用户在 dbt Model 中只写 Config 和查询内容：

```sql
-- models/mv_daily_sales.sql

{{ config(
    materialized='materialized_view',
    refresh_method='auto',
    refresh_trigger='schedule'
) }}

select
    order_date,
    sum(pay_amount) as sales_amount
from {{ ref('fact_orders') }}
group by order_date
```

其中，Config 告诉 dbt“把结果构建成异步物化视图”，下面的 `SELECT` 定义
“物化视图计算什么”。dbt-doris 负责把编译后的 `SELECT` 包装成类似下面的
Doris DDL：

```sql
CREATE MATERIALIZED VIEW `mv_daily_sales`
REFRESH AUTO
ON SCHEDULE ...
AS
select
    order_date,
    sum(pay_amount) as sales_amount
from `fact_orders`
group by order_date;
```

Hook 是通过 `pre_hook` 或 `post_hook` 配置，在 Model 构建前或构建后额外执行的
SQL。没有专用 Materialization 时，用户可能被迫使用下面这种临时绕法：

```sql
-- 仅用于说明 Hook 绕法，不是推荐写法

{{ config(
    pre_hook="DROP MATERIALIZED VIEW IF EXISTS mv_daily_sales",
    post_hook="CREATE MATERIALIZED VIEW mv_daily_sales REFRESH AUTO ON MANUAL AS SELECT ..."
) }}

select ...
```

这种方式要求用户自己拼接完整 DDL，并处理对象识别、重复运行、配置变化、重建和
删除。#65967 的目标就是让这些工作由 dbt-doris 完成，用户只维护正常的 Config
和 `SELECT`。

这里支持的是 **Doris Async Materialized View**，不是 Doris Sync Materialized
View，也不是把普通 dbt Table 改一个名字。

## 1. Issue 原文确认了什么

Issue 明确提出了以下需求：

1. 当前 dbt-doris 没有办法从 dbt 创建 Doris 异步物化视图；
2. 新增 dbt 的 `materialized_view` Materialization；
3. 该 Materialization 应生成 Doris
   `CREATE MATERIALIZED VIEW ... REFRESH ... AS ...`；
4. 最终用户目标是通过 dbt 管理 Doris 异步物化视图。

Issue 没有进一步定义：

- dbt Config 的具体名称；
- 默认刷新方式；
- 重复执行 `dbt run` 时如何处理已有定义；
- Model SQL 或刷新配置变化时怎样更新对象；
- `--full-refresh` 的行为；
- 如何从 Doris 元数据中识别异步物化视图；
- 支持哪些 Doris 版本；
- 是否等待 `BUILD IMMEDIATE` 产生的首次异步任务；
- 是否包含暂停、恢复、取消和刷新状态监控。

因此，“生成一条 CREATE SQL”是这个 Issue 的起点，不是完整的完成标准。

## 2. 为什么需要这个能力

### 2.1 当前三种常见 Model 物化方式不能替代它

| 物化方式 | 数据由谁维护 | 数据什么时候更新 | 与 Async MV 的差别 |
| --- | --- | --- | --- |
| View | 不保存结果数据 | 查询时读取最新底表 | 查询时仍要执行原始计算 |
| Table | dbt-doris | 每次 `dbt run` 重建 | 刷新调度仍由 dbt 或外部调度器负责 |
| Incremental | dbt-doris | 每次 `dbt run` 增量写入 | 增量边界和写入 SQL 由 dbt Model 负责 |
| Async Materialized View | Doris | 手动、定时或底表提交后触发 | Doris 保存预计算结果并管理刷新任务 |

Doris 异步物化视图适合：

- 对复杂聚合或 Join 结果做预计算；
- 加速报表和重复查询；
- 由 Doris 定时刷新，而不是依赖频繁执行 `dbt run`；
- 对分区表只刷新发生变化的分区；
- 参与 Doris 的透明查询改写。

### 2.2 dbt 在这里负责什么

dbt Core 负责：

- 解析 Model 和 `ref()` 依赖；
- 选择 `materialized_view` Materialization；
- 决定本次运行哪些 Model；
- 提供 `--full-refresh`、Hook 和配置变化等通用生命周期。

dbt-doris 负责：

- 识别 Doris 专用 Config；
- 生成 Doris Async MV SQL；
- 从 Doris 元数据中识别已有对象；
- 把 dbt 的创建、替换和删除动作以及刷新策略映射到 Doris DDL。

Doris 负责：

- 创建并保存异步物化视图；
- 按 DDL 策略创建和执行后续刷新任务；
- 选择刷新分区并维护分区刷新状态；
- 执行查询和透明查询改写。

## 3. 期望的用户使用方式

下面是为了把 Issue 变成可实现需求而给出的**建议接口**。Issue 原文没有确定这些
Config 名称，合入前仍需由维护者确认。

### 3.1 使用 `ON MANUAL` 刷新策略的异步物化视图

```sql
-- models/mv_daily_sales.sql

{{ config(
    materialized='materialized_view',
    build_mode='immediate',
    refresh_method='auto',
    refresh_trigger='manual',
    partition_by='order_date',
    distributed_by=['customer_id'],
    buckets=8,
    properties={
        'replication_num': '3',
        'workload_group': 'dbt_mv'
    }
) }}

select
    order_date,
    customer_id,
    sum(amount) as sales_amount
from {{ ref('fct_orders') }}
group by order_date, customer_id
```

期望生成的 Doris SQL 类似：

```sql
CREATE MATERIALIZED VIEW `analytics`.`mv_daily_sales`
BUILD IMMEDIATE
REFRESH AUTO ON MANUAL
PARTITION BY (`order_date`)
DISTRIBUTED BY HASH (`customer_id`) BUCKETS 8
PROPERTIES (
    "replication_num" = "3",
    "workload_group" = "dbt_mv"
)
AS
select
    order_date,
    customer_id,
    sum(amount) as sales_amount
from `analytics`.`fct_orders`
group by order_date, customer_id;
```

`dbt run` 负责部署以上定义和刷新策略，不执行
`REFRESH MATERIALIZED VIEW`。部署后的手动刷新、刷新范围和分区选择属于 Doris
运维范围。

### 3.2 定时刷新型异步物化视图

概念上还需要表达：

```sql
BUILD DEFERRED
REFRESH AUTO
ON SCHEDULE EVERY 1 DAY
STARTS '2026-08-01 02:00:00'
```

对应 Config 至少需要包含：

- 刷新间隔数值；
- 刷新时间单位；
- 可选的首次执行时间。

具体使用平铺 Config，还是一个结构化的 `refresh_schedule` Config，需要在实现前
确定。

## 4. Config 与 Doris SQL 的需求映射

| 功能 | 建议 Config | 可选值或格式 | Doris SQL |
| --- | --- | --- | --- |
| 对象类型 | `materialized` | `materialized_view` | `CREATE MATERIALIZED VIEW` |
| 首次构建 | `build_mode` | `immediate`、`deferred` | `BUILD IMMEDIATE/DEFERRED` |
| 刷新方法 | `refresh_method` | `complete`、`auto` | `REFRESH COMPLETE/AUTO` |
| 刷新触发 | `refresh_trigger` | `manual`、`schedule`、`commit` | `ON MANUAL/SCHEDULE/COMMIT` |
| 调度周期 | 待定 | 数值和 minute/hour/day/week 等单位 | `EVERY <n> <unit>` |
| 调度起点 | 待定 | Doris 可解析的时间 | `STARTS '<time>'` |
| Key | `duplicate_key` 或专用 MV Key Config | 列名或列名列表 | `DUPLICATE KEY (...)` |
| 分区 | `partition_by` | 列或 `date_trunc` 表达式 | `PARTITION BY (...)` |
| 分布 | `distributed_by` | Hash 列列表 | `DISTRIBUTED BY HASH (...)` |
| 随机分布 | 建议新增分布类型 Config | `random` | `DISTRIBUTED BY RANDOM` |
| Bucket | `buckets` | 正整数或 `auto` | `BUCKETS <n>/AUTO` |
| 说明 | Model Description | 字符串 | `COMMENT '<text>'` |
| 属性 | `properties` | 字典 | `PROPERTIES (...)` |

Config 设计必须满足：

1. 对枚举值做大小写归一和合法性校验；
2. 只在对应触发方式下接受调度参数；
3. 错误组合在编译或执行前给出清晰错误；
4. 不直接把未经校验的任意片段拼进 SQL；
5. 文档明确各配置要求的 Doris 最低版本。

例如，`ON COMMIT` 从 Doris 2.1.4 开始提供；使用该配置时不能只验证
dbt-doris 版本，还要验证或声明 Doris 版本边界。

## 5. 功能需求

### R1. 注册 `materialized_view` Materialization

用户只需配置：

```sql
{{ config(materialized='materialized_view') }}
```

不需要复制自定义 Macro，也不需要通过 Pre-hook 或 Post-hook 手写 DDL。

本任务针对当前 dbt-doris 的 dbt Core v1 Python Adapter。Issue 中引用了带
Fusion/v2 参数的 dbt 文档链接，但这不表示任务要求把 dbt-doris 迁移到 dbt v2。

### R2. 正确编译 Model SQL 和依赖

Materialized View 的 `AS <query>` 必须使用 dbt 编译后的 SQL，因此：

- `ref()` 能正确解析上游 Model；
- `source()` 能正确解析 Source；
- Model 在 dbt DAG 中保留正常的上下游关系；
- Alias、Schema 和 Target 环境规则继续生效。

### R3. 生成合法的 Doris Async MV DDL

创建语句必须：

- 明确生成异步物化视图语法；
- 正确引用 Database 和对象名；
- 按 Doris 语法顺序输出刷新策略、Key、分区、分布、属性和查询；
- 正确引用标识符并转义注释、属性值；
- 不把 Doris Sync MV 误当成此 Materialization 的实现。

### R4. 正确识别已有异步物化视图

dbt-doris 必须把已有 Doris Async MV 识别成
`RelationType.MaterializedView`，而不是普通 Table。

这是完整生命周期的前提。只有正确识别后，dbt 才知道应该执行：

```sql
DROP MATERIALIZED VIEW ...
SHOW CREATE MATERIALIZED VIEW ...
ALTER MATERIALIZED VIEW ...
REPLACE WITH MATERIALIZED VIEW ...
```

而不是错误地执行 `DROP TABLE` 或 Table 替换逻辑。

### R5. 定义重复执行行为

至少要保证：

- 第一次 `dbt run` 创建对象；
- 第二次运行不会因对象已存在而失败；
- 不会把已有 Async MV 当作 Table 删除；
- 不会在每次运行中无条件 Drop/Create；
- 定义未变化时跳过，不提交显式刷新。

dbt 官方把 Materialized View 的 `dbt run` 主要视为定义和配置的部署动作，
而数据刷新通常由数据库管理。dbt-doris 采用相同边界：Model 生命周期只部署
CREATE/REPLACE/DROP 和刷新策略 DDL，不提供让普通 `dbt run` 主动刷新或指定
刷新分区的 Model Config。后续刷新执行、触发时机和分区选择由 Doris 管理。

`BUILD IMMEDIATE` 是部署完成语义的特例：CREATE 新定义时，Adapter 等待 Doris
产生的首次构建任务成功，再把新定义视为可用；这不会把后续刷新纳入 dbt
生命周期。

### R6. 处理 SQL 和配置变化

需要支持 dbt 的 `on_configuration_change` 语义：

| 配置 | 期望行为 |
| --- | --- |
| `apply` | 能安全 ALTER 的配置直接修改；不能 ALTER 的变化安全重建 |
| `continue` | 保留已有对象，给出警告并继续 |
| `fail` | 检测到变化时明确失败，不修改已有对象 |

建议的变化处理原则：

- Refresh Method、Refresh Trigger 和部分 MV Properties 可评估使用
  `ALTER MATERIALIZED VIEW`；
- Model SQL、Key、Partition 和 Distribution 等结构变化通常需要重建；
- 重建应优先考虑创建临时 MV 后使用 Doris
  `REPLACE WITH MATERIALIZED VIEW`，减少直接删除旧对象的风险；
- 如果 Doris 版本不支持安全替换，行为必须被明确记录和测试。

### R7. 支持 `--full-refresh`

执行：

```bash
dbt run --full-refresh --select mv_daily_sales
```

应重新部署该异步物化视图的完整定义。

需要保证：

- 旧对象与新对象的类型判断正确；
- 构建失败时尽可能保留旧对象；
- 成功后不遗留临时或备份对象；
- Full Refresh 的含义是重建 dbt 对象定义，不要与 Doris
  `REFRESH ... COMPLETE` 混为同一个概念。

### R8. 支持正确的删除和类型切换

以下切换都必须有确定行为：

```text
不存在 -> Materialized View
Table -> Materialized View
View -> Materialized View
Materialized View -> Table
Materialized View -> View
Materialized View -> Materialized View
```

删除 Async MV 必须使用：

```sql
DROP MATERIALIZED VIEW IF EXISTS <name>;
```

不能依赖当前通用 `drop {{ relation.type }}` 在错误 Relation Type 下猜测对象类型。

### R9. 保持 dbt 通用生命周期

Materialization 至少应正确处理：

- Pre-hook；
- Post-hook；
- Adapter Commit；
- 返回正确的 Relation；
- 失败清理；
- dbt 日志中的主 Statement。

当前实现已把 `persist_docs.relation/columns` 和 Doris 专用 `grants` 纳入该
生命周期；创建、替换和幂等运行路径都必须保持相同行为。

### R10. 提供文档、示例和兼容性说明

用户文档至少需要说明：

- 最小可运行示例；
- Manual、Schedule、Commit 三种刷新方式；
- AUTO 与 COMPLETE 的区别；
- `dbt run` 不触发部署后的刷新；
- `--full-refresh` 与 `REFRESH ... COMPLETE` 的区别；
- Config 列表和默认值；
- Doris 与 dbt Core 的版本要求；
- 权限要求；
- 已知限制和排错方式。

## 6. 当前实现

### 6.1 Materialization 与 Relation 生命周期

Adapter 已注册 Doris 专用 `materialized_view` Materialization，并提供 Async MV
Create、Replace、Drop、Rename、Relation Type 识别和配置变化处理。Relation
Cache 会结合 `mv_infos` 把 Async MV 补全为 Materialized View，而不是依赖
`information_schema.tables` 中容易误判的 `BASE TABLE`。

Table、View 和 Async MV 之间切换时按真实对象类型使用对应 DDL。结构变化和
`--full-refresh` 先构建临时 MV；Immediate 首次构建成功后再使用 Doris
`REPLACE WITH MATERIALIZED VIEW` 原子替换，Deferred 按其语义不发起首次构建。
部署 Comment 的 Pending/Complete 标记与备份 Relation 用于识别和恢复中断部署。

### 6.2 Config、刷新策略与首次构建可观测性

`DorisConfig` 已声明 Build、Refresh Method、Refresh Trigger、Schedule、分区
定义、Distribution、Bucket、Properties、首次构建等待超时和轮询间隔等类型。
Jinja Macro 会归一化枚举值，校验不合法组合，并引用标识符和转义字符串。

默认 `BUILD IMMEDIATE` 会等待 CREATE 新定义产生的 Doris 首次构建任务成功。
定义未变化时跳过，不提交显式刷新；后续刷新与分区选择由 Doris 管理。Adapter
只从 `tasks('type'='mv')` 识别并等待首次构建任务；失败、取消、未知状态或超时
会使 Model 失败，成功 Adapter Response 包含 Task ID、Status 和可用的 Last
Query ID。

### 6.3 Docs、Grants 与 Hook

`persist_docs.relation` 控制 Relation Description 是否进入 MV Comment，
`persist_docs.columns` 通过 CREATE MV 完整列定义写入 Column Comment。只有启用
的 Description 会进入定义 Hash。

Doris 专用 Grants 使用显式 `role:<name>`、`user:<name>@<host>` Principal；
`grants_mode=replace` 收敛直接 Relation Grants，`additive` 只增加授权。项目级
`+grants` 可用于 MV，不再硬失败，也不会回收 Global/Database 等继承权限。
所有 Principal 在 MV DDL 前一次性校验，避免无效 Principal 暴露新定义或留下
部分授权；User 按大小写精确比较，Role 和 Host 按 Doris 语义比较。

Outside Pre-hook 在 `SHOW CREATE` 和配置漂移检查前执行；Inside Hook 参与实际
部署动作。Inside Post-hook 成功后才把部署标记从 Pending 改为 Complete；
Replace 后 Post-hook 失败时保留旧 MV，并在下次运行先原子回滚再重试。

### 6.4 版本矩阵

| Doris 版本线 | 支持范围 |
| --- | --- |
| 2.1 | 2.1.5 及之后的 2.1.x，包含 ON COMMIT |
| 3.0 | 3.0.1 及之后；排除 3.0.0 |
| 3.1 | 3.1.x |
| 4.x | 4.x |

Adapter 通过 `SHOW FRONTENDS` 校验当前连接 FE 和 Master FE；任一关键 FE
无法确定、无法解析或不在矩阵内时直接失败。3.0.0 缺少本生命周期依赖的原子
MV Replace 语义。生产 Schedule Unit 为 minute/hour/day/week；测试专用的
second 会被 Adapter 拒绝。

## 7. 交付范围结论

### 7.1 已交付

- Model SQL、`ref()`、`source()`、Alias 和环境 Schema；
- BUILD IMMEDIATE/DEFERRED；
- REFRESH AUTO/COMPLETE 与 ON MANUAL/SCHEDULE/COMMIT；
- BUILD IMMEDIATE 首次构建任务等待和 Doris Task 结果；
- Key、Partition、Distribution、Buckets、Properties 和 `replication_num`；
- 定义 Hash、幂等运行、`on_configuration_change`、Full Refresh 和原子替换；
- Relation/Column Persist Docs、Doris User/Role Grants 和 Hook 顺序；
- Unit Test、dbt 官方 Materialized View Contract Test、Doris Functional Test
  与用户文档。

### 7.2 不属于本 Issue 的内容

- Doris Sync Materialized View；
- dbt v2/Fusion Adapter 迁移；
- Doris Async MV 内核实现；
- 透明改写规则本身的增强；
- 通用任务调度平台；
- 全部 External Catalog 能力；
- Async MV 的可视化运维平台。

## 8. 验收标准

### AC1. 首次创建

给定一个引用上游 Model 的 `materialized_view` Model，执行：

```bash
dbt run --select mv_daily_sales
```

结果应为：

- 命令成功；
- Doris 中存在 Async MV；
- `SHOW CREATE MATERIALIZED VIEW` 包含期望的 Model SQL 和配置；
- `mv_infos` 能查询到该对象；
- dbt 返回的 Relation Type 是 Materialized View。

### AC2. 重复运行

连续执行两次相同的 `dbt run`：

- 第二次不因对象存在而失败；
- 不错误执行 `DROP TABLE`；
- 不遗留临时或备份对象；
- 不提交 `REFRESH MATERIALIZED VIEW`，后续刷新仍由 Doris 管理。

### AC3. 三种刷新触发

分别验证：

```text
ON MANUAL
ON SCHEDULE EVERY ...
ON COMMIT
```

`SHOW CREATE MATERIALIZED VIEW` 或 `mv_infos.RefreshInfo` 必须与 Config 一致。

### AC4. AUTO 和 COMPLETE

分别配置 AUTO、COMPLETE：

- CREATE 和 `SHOW CREATE MATERIALIZED VIEW` 中的刷新策略正确；
- 后续刷新范围由 Doris 按该策略选择；
- 非法值在执行前失败并指出具体 Config。

### AC5. 配置变化

修改 Refresh、Property、Partition 或 Distribution：

- `apply`、`continue`、`fail` 行为分别符合文档；
- 需要重建的变化不被错误地当作数据刷新；
- 失败时不静默丢失旧对象。

### AC6. Model SQL 变化

修改 Model 的 SELECT：

- dbt 能检测定义变化；
- 新定义最终反映到 `SHOW CREATE MATERIALIZED VIEW`；
- 必须部署新定义，不能把定义变化当作数据刷新。

### AC7. Full Refresh

执行 `dbt run --full-refresh`：

- 对象定义被完整重建；
- 与 Doris `REFRESH ... COMPLETE` 的行为区分清楚；
- 不遗留临时对象。

### AC8. 类型切换

验证 Table、View 和 Materialized View 之间的切换：

- 使用正确的 Drop DDL；
- 最终对象类型正确；
- dbt Relation Cache 不保留错误类型。

### AC9. 依赖和环境

- `ref()`、`source()` 正确编译；
- Dev/Prod Schema 隔离正确；
- Alias 正确；
- `dbt ls` 和 Docs 中仍能看到正常依赖。

### AC10. 测试要求

至少包含：

- Macro SQL 生成 Unit Test；
- Config 合法值和非法组合 Unit Test；
- Relation Type 识别 Unit Test；
- 创建、重复运行、刷新策略、首次构建任务、Full Refresh 和类型切换
  Functional Test；
- 有 Doris 版本边界的兼容性测试或明确跳过条件。

## 9. 已确定的实现决策

| 问题 | 当前决策 |
| --- | --- |
| 默认 Build Mode | `BUILD IMMEDIATE` |
| 默认 Refresh Method | `AUTO` |
| 默认 Trigger | `ON MANUAL` |
| 无变化的 `dbt run` | Skip，不提交 `REFRESH MATERIALIZED VIEW` |
| Schedule Config | `refresh_schedule={interval, unit, start_time?}`；拒绝测试专用的 second |
| Async MV 识别 | 结合 `mv_infos` 补全 Relation Type |
| SQL/Config 变化 | 临时 MV 构建成功后原子 Replace |
| 首次构建完成语义 | 只等待 `BUILD IMMEDIATE` 首次 Task SUCCESS，并把任务信息放入 Adapter Response |
| 后续刷新与分区选择 | 由 Doris 按 MV DDL 管理 |
| Docs | 支持 `persist_docs.relation` 与 `persist_docs.columns` |
| Grants | 显式 User/Role Principal；Replace 或 Additive |
| 最低 Doris 版本 | 2.1.5+、3.0.1+、3.1.x、4.x，排除 3.0.0 |

## 10. 实现拆分结果

以下交付顺序已经完成：

1. Relation 元数据识别与 Drop；
2. 最小 `materialized_view` 创建；
3. Build、Refresh Method/Trigger、Schedule 和分区定义 Config；
4. 重复运行、首次构建任务结果和 `--full-refresh`；
5. 配置差异、原子 Replace、Hook、Docs、Grants 和异常恢复；
6. Unit Test、官方 Contract Test、Doris Functional Test 和用户文档。

## 11. 参考资料

- [Apache Doris Issue #65967](https://github.com/apache/doris/issues/65967)
- [dbt Materializations](https://docs.getdbt.com/docs/build/materializations)
- [Doris CREATE ASYNC MATERIALIZED VIEW](https://doris.apache.org/docs/4.x/sql-manual/sql-statements/table-and-view/async-materialized-view/CREATE-ASYNC-MATERIALIZED-VIEW/)
- [Doris Async Materialized View 管理与查询](https://doris.apache.org/docs/4.x/query-acceleration/materialized-view/async-materialized-view/functions-and-demands/)
- [Doris MV_INFOS](https://doris.apache.org/docs/4.x/sql-manual/sql-functions/table-valued-functions/mv_infos/)
- [Doris REFRESH MATERIALIZED VIEW](https://doris.apache.org/docs/dev/sql-manual/sql-statements/table-and-view/async-materialized-view/REFRESH-MATERIALIZED-VIEW)
- [Doris ALTER ASYNC MATERIALIZED VIEW](https://doris.apache.org/docs/dev/sql-manual/sql-statements/table-and-view/async-materialized-view/ALTER-ASYNC-MATERIALIZED-VIEW)
- [Doris DROP ASYNC MATERIALIZED VIEW](https://doris.apache.org/docs/4.x/sql-manual/sql-statements/table-and-view/async-materialized-view/DROP-ASYNC-MATERIALIZED-VIEW/)
