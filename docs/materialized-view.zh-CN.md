# 在 dbt-doris 中使用 Doris 异步物化视图

dbt Model 配置 `materialized='materialized_view'` 后，dbt-doris 会把编译后的
Model 查询创建为 Doris Async Materialized View。`ref()`、`source()`、Alias、
目标 Schema、Hook、血缘、测试和文档仍由 dbt 管理，刷新任务由 Doris 执行。

本 Materialization **只管理 Doris 异步物化视图**。Doris Sync Materialized
View（Rollup）具有不同的 DDL 和生命周期，不在本实现范围内。

## 版本范围

| Doris 版本线 | Async MV 支持范围 |
| --- | --- |
| 2.1 | 2.1.5 及之后的 2.1.x，包含 `ON COMMIT` |
| 3.0 | 3.0.1 及之后；明确排除 3.0.0 |
| 3.1 | 3.1.x |
| 4.x | 4.x |

dbt Core 的开发与测试基线是 1.12.x。每次管理 Async MV 前，Adapter 会从
`SHOW FRONTENDS` 读取当前连接 FE 和 Master FE 的版本；任一关键 FE 无法确定、
无法解析或不在矩阵内时直接失败。Doris 3.0.0 缺少本生命周期依赖的原子 MV
Replace 语义，因此明确拒绝。

生产定时任务支持 `minute`、`hour`、`day` 和 `week`。Adapter 会拒绝
`second`，因为 Doris 只通过测试专用设置开启秒级 Schedule。

## 最小示例

```sql
-- models/daily_sales.sql
{{ config(
    materialized='materialized_view',
    replication_num='3'
) }}

select
    order_date,
    sum(amount) as sales
from {{ ref('orders') }}
group by order_date
```

未指定其他配置时，核心 DDL 相当于：

```sql
CREATE MATERIALIZED VIEW `analytics`.`daily_sales`
BUILD IMMEDIATE
REFRESH AUTO ON MANUAL
DISTRIBUTED BY RANDOM BUCKETS AUTO
PROPERTIES ("replication_num" = "3")
AS
select ...;
```

默认 `refresh_on_run=false`。首次 `dbt run` 创建定义并等待
`BUILD IMMEDIATE` 的刷新任务成功；以后定义未变化时保持幂等，不重复创建，
也不主动刷新。

## 配置

| Config | 默认值 | 支持值或格式 | 作用 |
| --- | --- | --- | --- |
| `build_mode` | `immediate` | `immediate`、`deferred` | 创建后立即构建，或推迟到以后刷新 |
| `refresh_method` | `auto` | `auto`、`complete` | 让 Doris 自动选择刷新范围，或强制完整刷新 |
| `refresh_trigger` | `manual` | `manual`、`schedule`、`commit` | 手动、定时或底表提交后触发 |
| `refresh_schedule` | 无 | `interval`、`unit`、可选 `start_time` | 仅用于 `schedule`；生产 Unit 为 minute/hour/day/week |
| `refresh_on_run` | `false` | Boolean | 定义不变时，每次 `dbt run` 是否主动提交刷新 |
| `refresh_partitions` | 无 | 分区名或分区名列表 | 分区 MV 在 `refresh_on_run=true` 时生成 `PARTITION(S)` |
| `wait_for_refresh` | `true` | Boolean | 是否等待本次 Adapter 提交的刷新任务结束 |
| `refresh_wait_timeout` | `300` | 正整数秒 | 等待刷新任务的总超时 |
| `refresh_poll_interval` | `1` | 正整数秒 | 查询 Doris Task 状态的间隔，不能大于总超时 |
| `duplicate_key` | 无 | 列名或列名列表 | 生成 `DUPLICATE KEY` |
| `partition_by` | 无 | 字符串或单元素列表 | 一个分区列或 Doris 支持的分区映射函数 |
| `distribution_type` | 自动判断 | `hash`、`random` | 设置分布方式 |
| `distributed_by` | 无 | 列名或列名列表 | Hash 分布列；配置后默认选择 Hash |
| `buckets` | `auto` | 正整数、`auto` | Bucket 数量 |
| `replication_num` | 无 | 正整数或数字字符串 | 合并进 Properties，并覆盖其中同名键 |
| `properties` | `{}` | 标量值字典 | Doris Async MV Properties |
| `on_configuration_change` | `apply` | `apply`、`continue`、`fail` | 已部署定义发生变化时的策略 |
| `grants_mode` | `replace` | `replace`、`additive` | 收敛或只增加直接 Relation Grants |

Schedule 示例：

```sql
{{ config(
    materialized='materialized_view',
    build_mode='deferred',
    refresh_method='auto',
    refresh_trigger='schedule',
    refresh_schedule={
        'interval': 1,
        'unit': 'day',
        'start_time': '2026-08-01 02:00:00'
    },
    distribution_type='hash',
    distributed_by=['customer_id'],
    buckets=8,
    replication_num='3'
) }}

select
    order_date,
    customer_id,
    sum(amount) as sales
from {{ ref('orders') }}
group by order_date, customer_id
```

`refresh_schedule` 不能用于 `manual` 或 `commit`，`unit='second'` 会在执行
DDL 前被拒绝。

## 主动刷新、指定分区和任务结果

定义未变化时，可通过下面的配置让本次 `dbt run` 主动刷新：

```sql
{{ config(
    materialized='materialized_view',
    refresh_on_run=true,
    partition_by='order_date',
    refresh_partitions=['p202607', 'p202608'],
    wait_for_refresh=true,
    refresh_wait_timeout=600,
    refresh_poll_interval=2
) }}
```

生成的核心 SQL 是：

```sql
REFRESH MATERIALIZED VIEW `analytics`.`daily_sales`
PARTITIONS (`p202607`, `p202608`);
```

没有 `refresh_partitions` 时，主动刷新使用 `refresh_method` 对应的
`AUTO` 或 `COMPLETE`。分区名由 Adapter 作为标识符引用，不接受任意 SQL
表达式。`refresh_partitions` 必须同时配置 `partition_by` 和
`refresh_on_run=true`；否则 Adapter 会在提交 SQL 前给出明确错误。

刷新是 Doris 异步任务，但 Adapter 默认不会只确认 SQL 提交成功：

1. 提交前记录该 MV 已有 Task ID；
2. 提交后轮询 `tasks('type'='mv')` 中本次新增任务；
3. `SUCCESS` 才完成 Model，并在 dbt Adapter Response 中返回 Task ID、
   Status，以及 Doris 提供时的 Last Query ID；
4. `FAILED`、`CANCELED`、未知状态或超时都会让 Model 失败，并携带任务错误。

只有明确配置 `wait_for_refresh=false` 时才不等待。等待依赖 Doris 保留 MV
Task History；若任务历史被关闭或过早清理，Adapter 会超时并给出提示。

## `dbt run` 如何处理已有对象

| 场景 | 行为 |
| --- | --- |
| 目标不存在 | 创建异步物化视图 |
| 定义未变化 | 跳过；`refresh_on_run=true` 时主动刷新 |
| Model SQL、Persisted Docs 或 MV DDL Config 变化 | 按 `on_configuration_change` 处理 |
| `on_configuration_change='apply'` | 构建临时 MV；Immediate 等刷新成功后原子 Replace，Deferred 不等待首次刷新 |
| `on_configuration_change='continue'` | 保留 Doris 中的旧定义并给出警告 |
| `on_configuration_change='fail'` | 终止运行，不修改已有对象 |
| 使用 `--full-refresh` | 忽略变化策略，重新部署完整 MV 定义 |
| Table、View 与 MV 互相切换 | 按真实 Relation Type 执行备份、创建、改名和清理 |

dbt-doris 在 MV Comment 中保存部署状态和定义 Hash。Hash 对 SQL 与等价配置做
归一化，避免字符串/列表写法、大小写或 Property 顺序引起无意义重建。部署先写
`deployment-pending`，Inside Post-hook 成功后才改成
`definition-hash`；如果进程在中途失败，下次运行会识别未完成部署并安全恢复。
如果原子 Replace 已完成但 Inside Post-hook 失败，旧 MV 会保留在临时名称下；
下一次运行先把旧 MV 原子换回线上目标，再重试新定义，避免过早删除最后一个完整
版本。

已有 MV 的结构变化不会先删除线上对象。Adapter 先创建临时 MV；Immediate 等待
刷新成功后再用 Doris 原子 Swap 暴露新定义，Deferred 按其语义不提交首次刷新。
类型切换使用备份 Relation，失败时尽量保留旧对象。
残留的 `__dbt_tmp` 或 `__dbt_backup` 对象会在后续运行中按部署状态恢复或清理。

Outside-transaction Pre-hook 在 `SHOW CREATE MATERIALIZED VIEW` 和定义漂移检查前
执行，因此 Hook 设置的 Session 状态可影响这些元数据查询。Inside Hook 只在实际
创建、替换或刷新动作中执行；Outside Post-hook 在清理流程之后执行。

`--full-refresh` 是重新部署 MV 定义，不等于只执行
`REFRESH MATERIALIZED VIEW ... COMPLETE`。

## Persist Docs

Relation 和 Column Description 均可持久化：

```yaml
models:
  - name: daily_sales
    description: 每日销售汇总
    config:
      persist_docs:
        relation: true
        columns: true
    columns:
      - name: order_date
        description: 订单日期
      - name: sales
        description: 销售额
```

- `persist_docs.relation=true` 时，Relation Description 与 Adapter 部署标记一起
  写入 MV Comment；关闭时只保留部署标记。
- `persist_docs.columns=true` 时，Adapter 先读取 Model 查询的输出 Schema，再在
  `CREATE MATERIALIZED VIEW (...)` 的完整列定义中写入匹配的 Column Comment。
- 开启相应 Persist Docs 后，Description 变化会进入定义 Hash，并按配置变化策略
  重新部署；未开启的 Description 变化不会触发重建。
- YAML 中有说明但 Model 查询不存在的列会发出明确 Warning，避免静默漏写文档。

## Grants

Doris 的 User Identity 带 Host，而且 User 与 Role 可能同名，因此 Principal 必须
显式写类型：

```yaml
models:
  your_project:
    daily_sales:
      +grants:
        select:
          - "role:analyst"
          - "user:reporter@%"
          - "user:domain_reader@[example.com]"
      +grants_mode: replace
```

| Principal | 含义 |
| --- | --- |
| `role:<name>` | Doris Role |
| `user:<name>@<host>` | Doris User Identity |
| `user:<name>@[<domain>]` | Doris Domain User Identity |

裸名字不会被猜测成 User 或 Role。MV/View 支持 Relation 级 `select`
（Doris `SELECT_PRIV`）；`insert` 只适用于 Table，并映射为 Doris
`LOAD_PRIV`。

- `grants_mode='replace'`：比较该 Relation 上的直接授权，补齐缺少的授权后再
  回收配置中已删除的授权。
- `grants_mode='additive'`：只增加配置中的授权，不回收已有授权。

Replace 只管理目标 Relation 的直接 Table Privileges，不撤销从 Global、
Catalog、Database 或其他 Role 继承的权限。创建、替换、刷新、跳过和
`on_configuration_change='continue'` 路径都会应用 Grants，因此项目级
`+grants` 不会让 MV 编译失败，也不会在幂等运行时被忽略。

所有模式都会先用 `SHOW ROLES` 一次性验证配置中的 User/Role；MV 在任何创建、
替换或刷新 DDL 前完成该预检。不存在的 Principal 会让 Model 失败，且不会暴露
新的 MV 定义或执行部分授权。Doris User 名按大小写精确匹配，Role 和 Host
按 Doris 的大小写规则比较。

执行 dbt 的 Doris 身份必须能执行 `SHOW FRONTENDS`，并具有查询、创建、删除、
修改 MV 和管理目标授权所需的权限。配置 Grants 时还需读取 `SHOW ROLES`
（Doris 要求执行身份具备全局 `GRANT_PRIV`）；若执行身份没有该权限，请不要在
该 Model 上配置由 Adapter 管理的 Grants。

## 排错

- `partition_by` 只接受一个分区标识符或 Doris 支持的分区映射函数；多列或任意
  SQL 片段会在 Adapter 校验阶段失败。
- `refresh_partitions` 是 Doris 分区名，不是 Model 输出列名或分区表达式。
- 单 BE 开发集群应设置 `replication_num=1`；顶层值优先于
  `properties.replication_num`。
- 刷新超时时先检查 `tasks('type'='mv')`、Task History 保留设置和 Doris 返回的
  ErrorMsg/LastQueryId。
- `refresh_trigger='commit'` 仅在底表变更满足 Doris ON COMMIT 语义时触发，
  Adapter 不模拟 Commit 调度。
- 不要手动删除或修改 MV Comment 中的 `dbt-doris:` 部署标记。
