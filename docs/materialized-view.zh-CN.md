# dbt-doris 异步物化视图：当前实现与使用指南

dbt Model 配置 `materialized='materialized_view'` 后，dbt-doris 会把编译后的
Model 查询创建为 Doris Async Materialized View。`ref()`、`source()`、Alias、
目标 Schema、Hook、血缘、测试和文档仍由 dbt 管理。dbt-doris 部署
CREATE/REPLACE/DROP 和刷新策略 DDL；对于 `ON MANUAL`，定义未变化的后续
`dbt run` 还会提交一次 Doris Refresh。`ON SCHEDULE` 和 `ON COMMIT` 的后续
触发仍由 Doris 管理。

本 Materialization **只管理 Doris 异步物化视图**。Doris Sync Materialized
View（Rollup）具有不同的 DDL 和生命周期，不在本实现范围内。

## 先看结论

当前实现把 `dbt run` 定义为 **MV 定义和配置的部署动作，以及 ON MANUAL 的
刷新入口**。

这里的 `ON MANUAL` 不是“dbt 只把策略写进 DDL，之后完全不管刷新”：首次
Create/Replace 完成后，只要已部署定义没有变化，之后每次选中该 Model 的
`dbt run` 都会由 Adapter 提交一次 Doris
`REFRESH MATERIALIZED VIEW ... AUTO|COMPLETE`。

| 内容 | 由谁负责 |
| --- | --- |
| Model SQL、`ref()`、`source()`、Alias、Schema 和可选 Hook | 用户声明，dbt 编译 |
| CREATE、Replace、类型切换及部署流程所需的 Drop/失败恢复 | dbt-doris Adapter |
| `BUILD IMMEDIATE` 创建/替换的首次构建任务 | Doris 执行，Adapter 默认等待；不会额外提交 Refresh |
| `AUTO/COMPLETE` | 刷新范围：Doris 自动选择范围或执行完整刷新 |
| `MANUAL/SCHEDULE/COMMIT` | 刷新触发方式 |
| 定义未变的 `ON MANUAL` | Adapter 提交 Refresh，默认等待新 Task |
| 定义未变的 `ON SCHEDULE/COMMIT` | Adapter Skip，Doris 按 DDL 触发 |

因此：

- 首次运行会创建 MV；默认 `BUILD IMMEDIATE`，Adapter 等待首次构建成功。
- 首次创建或替换只等待 `BUILD IMMEDIATE` 自己产生的 Task，不会紧接着再提交
  一次 `REFRESH MATERIALIZED VIEW`。
- 定义未变化时，`ON MANUAL` 的后续 `dbt run` 提交
  `REFRESH MATERIALIZED VIEW ... AUTO|COMPLETE`；默认等待本次新 Task。
- 定义未变化时，`ON SCHEDULE` 和 `ON COMMIT` Skip，把后续触发交给 Doris。
- `BUILD DEFERRED + ON MANUAL` 第一次运行只创建；第二次定义未变的运行提交
  第一次 Refresh。
- SQL 或配置变化时按 `on_configuration_change` 处理；目标已经是 MV、使用默认
  `apply` 且保持默认等待时，Adapter 构建临时 MV，首次构建成功后原子替换。
- 当前不提供指定分区刷新或内置刷新 `run-operation`。

## 版本范围

| Doris 版本 | 当前运行时 Gate |
| --- | --- |
| 2.x | 版本号不低于 2.1.5；Gate 单测覆盖 2.1.5 和 2.1.10 |
| 3.x | 除 3.0.0 外均通过 Gate；Gate 单测覆盖 3.0.1 和 3.1.0 |
| 4 及更高主版本 | 当前 Gate 接受；Gate 单测覆盖 4.1.2 |

dbt Core 的开发与测试基线是 1.12.x。每次管理 Async MV 前，Adapter 会从
`SHOW FRONTENDS` 优先读取当前连接 FE 和 Master FE 的版本；如果返回结果无法
标出这两个角色，则退回校验第一行。被选中行的版本无法解析或未通过 Gate 时
直接失败。Doris 3.0.0 缺少本生命周期依赖的原子 MV Replace 语义，因此明确
拒绝。

上表描述的是当前代码中的版本判断条件，不等于对尚未实际测试的未来 Doris
版本作兼容性保证。

生产定时任务支持 `minute`、`hour`、`day` 和 `week`。Adapter 会拒绝
`second`，因为 Doris 只通过测试专用设置开启秒级 Schedule。

## 用户最少需要写什么

最小模型只需要 `materialized='materialized_view'` 和查询 SQL：

```sql
-- models/daily_sales.sql
{{ config(
    materialized='materialized_view',
    replication_num='1'
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
PROPERTIES ("replication_num" = "1")
AS
select ...;
```

这里使用 `replication_num='1'` 方便单 BE 开发环境直接运行；生产环境应按 Doris
集群副本策略调整。

正常创建和更新流程中，用户不需要手写 CREATE、Replace、生命周期 Drop、Task
轮询或失败恢复 SQL。在 dbt Model 定义中，除查询外，只有 Hook 是用户可选的
原始 SQL；刷新策略、Docs 和 Grants 都通过 Config 声明，由 Adapter 生成对应
语句。`ON MANUAL` 的后续刷新由定义未变化时的下一次 `dbt run` 提交；用户仍可
按需直接执行 Doris 原生 Refresh SQL。

运行 Model：

```bash
dbt run --select daily_sales
```

首次 `dbt run` 创建定义并等待 `BUILD IMMEDIATE` 产生的首次任务成功，不额外
提交 Refresh。以后定义未变化时不重复创建：默认 `ON MANUAL` 会提交一次
`REFRESH MATERIALIZED VIEW ... AUTO` 并等待；`ON SCHEDULE` 或 `ON COMMIT`
则 Skip，由 Doris 按 DDL 中的策略管理。

## 刷新方式怎么选择

### `refresh_trigger`

| 配置 | 谁触发后续刷新 | 适用场景 |
| --- | --- | --- |
| `manual` | 定义未变化的 `dbt run` 提交 Doris Refresh；也可直接执行原生 SQL | 由 dbt Job 或外部调度精确触发 |
| `schedule` | Doris 内置 Schedule | 固定时间间隔刷新 |
| `commit` | Doris 根据底表提交触发 | 希望底表变化后自动刷新，且满足 Doris `ON COMMIT` 约束 |

默认是 `manual`，同时默认 `build_mode='immediate'`。使用这两个默认值时，第一
次 `dbt run` 只创建并等待首次构建；第二次及以后定义未变化的 `dbt run` 会
提交 Refresh。

#### Manual

Model 配置：

```sql
{{ config(
    materialized='materialized_view',
    build_mode='immediate',
    refresh_method='auto',
    refresh_trigger='manual'
) }}

select ...
```

第一次运行创建 MV。以后定义未变化时，每次执行：

```bash
dbt run --select daily_sales
```

Adapter 会提交：

```sql
REFRESH MATERIALIZED VIEW `analytics`.`daily_sales` AUTO;
```

配置 `refresh_method='complete'` 时提交：

```sql
REFRESH MATERIALIZED VIEW `analytics`.`daily_sales` COMPLETE;
```

具体使用 `AUTO` 还是 `COMPLETE` 取决于 `refresh_method`。默认
`wait_for_refresh=true`，Adapter 会等待这次新 Task；设置为 `false` 时只提交
Refresh SQL，不轮询结果。

也可以从 MySQL Client、Doris SQL Console、Airflow 或其他调度系统直接执行上述
Doris 原生 SQL；这不改变 dbt Model 的生命周期语义。

刷新是 Doris 异步任务，可以在 Doris 中查看：

```sql
select TaskId, Status, ErrorMsg, LastQueryId
from tasks('type'='mv')
where MvDatabaseName = 'analytics'
  and MvName = 'daily_sales'
order by CreateTime desc, TaskId desc;
```

#### Schedule

```sql
{{ config(
    materialized='materialized_view',
    refresh_method='auto',
    refresh_trigger='schedule',
    refresh_schedule={
        'interval': 1,
        'unit': 'day',
        'start_time': '2026-08-01 02:00:00'
    }
) }}

select ...
```

这会把类似下面的策略写入 CREATE DDL：

```sql
REFRESH AUTO
ON SCHEDULE EVERY 1 DAY
STARTS '2026-08-01 02:00:00'
```

`start_time` 可省略。生产 Schedule Unit 支持 `minute`、`hour`、`day` 和
`week`。定义未变化的后续 `dbt run` 不会再提交 Refresh，而是 Skip 并把触发
交给 Doris Schedule。

#### Commit

```sql
{{ config(
    materialized='materialized_view',
    refresh_method='auto',
    refresh_trigger='commit'
) }}

select ...
```

Adapter 只生成 `REFRESH AUTO ON COMMIT`；是否以及何时产生刷新任务由 Doris
判断，Model 和底表必须满足 Doris 的 `ON COMMIT` 使用约束。定义未变化的
后续 `dbt run` Skip，不额外提交 Refresh。

### `refresh_method`

`refresh_method` 与触发方式相互独立：

| 配置 | 含义 |
| --- | --- |
| `auto` | Doris 根据能够获取的底表快照和分区版本判断刷新范围；对于可跟踪的底表尽量只刷新变化分区 |
| `complete` | 不检查分区是否已同步，强制刷新 MV 的全部分区 |

`AUTO` 只表示“由 Doris 选择刷新范围”，不表示“自动触发刷新”。是否自动触发由
`refresh_trigger` 决定。对于 Doris 无法感知数据变化的外表（例如 JDBC），
`AUTO` 可能把 MV 视为已经同步，而不是可靠地退化为全量刷新；这类场景应使用
`COMPLETE`。

## 完整配置参考

| Config | 默认值 | 支持值或格式 | 作用 |
| --- | --- | --- | --- |
| `build_mode` | `immediate` | `immediate`、`deferred` | 创建后立即构建，或推迟到以后刷新 |
| `refresh_method` | `auto` | `auto`、`complete` | 刷新范围；同时用于 DDL 和 Adapter 提交的 Manual Refresh |
| `refresh_trigger` | `manual` | `manual`、`schedule`、`commit` | 刷新触发方式；Manual 由定义未变的 dbt run 提交 |
| `refresh_schedule` | 无 | `interval`、`unit`、可选 `start_time` | 仅用于 `schedule`；生产 Unit 为 minute/hour/day/week |
| `wait_for_refresh` | `true` | Boolean | 是否等待首次构建或 Adapter 提交的 Manual Refresh Task |
| `refresh_wait_timeout` | `300` | 正整数秒 | 等待本次 Refresh Task 的总超时 |
| `refresh_poll_interval` | `1` | 正整数秒 | 查询本次 Task 状态的间隔，不能大于总超时 |
| `duplicate_key` | 无 | 列名或列名列表 | 生成 `DUPLICATE KEY` |
| `partition_by` | 无 | 字符串或单元素列表 | 一个分区列或 Doris 支持的分区映射函数 |
| `distribution_type` | 自动判断 | `hash`、`random` | 设置分布方式 |
| `distributed_by` | 无 | 列名或列名列表 | Hash 分布列；配置后默认选择 Hash |
| `buckets` | `auto` | 正整数、`auto` | Bucket 数量 |
| `replication_num` | 无 | 正整数或数字字符串 | 合并进 Properties，并覆盖其中同名键 |
| `properties` | `{}` | 标量值字典 | Doris Async MV Properties |
| `on_configuration_change` | `apply` | `apply`、`continue`、`fail` | 已部署定义发生变化时的策略 |
| `grants_mode` | `replace` | `replace`、`additive` | 收敛或只增加直接 Relation Grants |

`refresh_schedule` 不能用于 `manual` 或 `commit`，`unit='second'` 会在执行
DDL 前被拒绝。

## 首次构建与 Manual Refresh Task

| `build_mode` | 创建时行为 |
| --- | --- |
| `immediate`（默认） | Doris 立即构建新 MV；Adapter 默认等待成功后再暴露目标 |
| `deferred` | 创建时不构建；后续由 Manual、Schedule 或 Commit 产生第一次刷新 |

组合后的行为是：

| Build + Trigger | 首次构建 | 后续刷新 |
| --- | --- | --- |
| `IMMEDIATE + MANUAL` | 第一次 run 创建并等待 BUILD Task，不额外 Refresh | 定义未变的后续 run 提交 Refresh |
| `IMMEDIATE + SCHEDULE` | 第一次 run 创建并等待 BUILD Task | 后续 run Skip，Doris 按 Schedule 刷新 |
| `IMMEDIATE + COMMIT` | 第一次 run 创建并等待 BUILD Task | 后续 run Skip，Doris 按底表 Commit 刷新 |
| `DEFERRED + MANUAL` | 第一次 run 只创建，不产生 Task | 第二次定义未变的 run 提交第一次 Refresh |
| `DEFERRED + SCHEDULE` | 第一次 run 只创建 | 后续 run Skip，等待第一个 Schedule 周期 |
| `DEFERRED + COMMIT` | 第一次 run 只创建 | 后续 run Skip，等待第一次符合条件的底表提交 |

Adapter 会等待两类由当前 Model Action 产生的 Task：

- 创建或替换新定义时，`BUILD IMMEDIATE` 自己产生的首次 Task；
- 定义未变化且 Trigger 为 `ON MANUAL` 时，Adapter 显式提交 Refresh 产生的新
  Task。

创建和替换不会在等待 `BUILD IMMEDIATE` 后再提交第二个 Refresh。任务识别流程
为：

1. 在提交 CREATE 或 Manual Refresh 前记录该 MV 已有 Task ID；
2. 动作提交后轮询 `tasks('type'='mv')`，选择排序后第一个不在旧 ID 集合中的
   Task；
3. `SUCCESS` 才完成动作，并在 dbt Adapter Response 中返回 Task ID、Status，
   以及 Doris 提供时的 Last Query ID；
4. `FAILED`、`CANCELED`、未知状态或超时都会让 Model 失败，并携带任务错误。

当前等待器没有把 Refresh 语句的 Query ID 与 Task 做强关联。同一 MV 在 dbt
等待期间如果又被其他客户端并发刷新，Adapter 可能识别到另一项新 Task。因此
同一个 MV 应避免并发提交 Manual Refresh。

只有明确配置 `wait_for_refresh=false` 时才只提交、不等待。
`refresh_wait_timeout` 和 `refresh_poll_interval` 同时控制首次构建与 Manual
Refresh 的等待。等待依赖 Doris 保留 MV Task History；若任务历史被关闭或过早
清理，Adapter 会超时并给出提示。等待超时只会让 dbt Model 失败，不会取消已经
提交到 Doris 的异步 Refresh Task。

配置 `BUILD IMMEDIATE` 但设置 `wait_for_refresh=false` 时，Adapter 会在首次
任务完成前暴露新定义。下游 Model 可能看到尚未完成构建的 MV，只应在明确接受
该风险时使用。对于定义未变化的 Manual MV，关闭等待只提交 Refresh SQL，Model
成功不代表 Refresh Task 已完成。

## `dbt run` 如何处理已有对象

| 场景 | 行为 |
| --- | --- |
| 目标不存在 | 创建异步物化视图；Immediate 只等待 BUILD Task，Deferred 只创建 |
| 定义未变化 + `ON MANUAL` | 提交 `REFRESH MATERIALIZED VIEW ... AUTO/COMPLETE`；默认等待新 Task |
| 定义未变化 + `ON SCHEDULE/COMMIT` | Skip，不提交 Refresh；版本检查、Outside Hook 和可选 Grants 仍执行 |
| Model SQL、Persisted Docs 或 MV DDL Config 变化 | 按 `on_configuration_change` 处理 |
| `on_configuration_change='apply'` | 构建临时 MV；Immediate 默认等首次构建成功后原子 Replace，关闭等待时提前暴露，Deferred 不产生首次任务 |
| `on_configuration_change='continue'` | 保留 Doris 中的旧定义并给出警告；不提交 Manual Refresh、不执行 Inside Hook，但仍处理 Outside Hook 和 Grants |
| `on_configuration_change='fail'` | 终止运行，不修改已有对象 |
| 使用 `--full-refresh` | 即使定义未变也重新部署完整定义；只处理 BUILD，不额外提交 Manual Refresh |
| Table、View 与 MV 互相切换 | 交给目标 Materialization 按真实 Relation Type 处理；各方向的安全边界见下文 |

### MV → MV 原子替换与失败恢复

dbt-doris 在 MV Comment 中保存部署状态和定义 Hash。Hash 会归一化受支持的
枚举值、部分等价的字符串/列表配置、Buckets 和 Property 顺序；编译 SQL 的
大小写或内部空白变化仍可能改变 Hash，并触发重建。部署先写
`deployment-pending`，Inside Post-hook 成功后才改成
`definition-hash`；如果进程在中途失败，下次运行会识别未完成部署并安全恢复。
如果原子 Replace 已完成但 Inside Post-hook 失败，旧 MV 会保留在临时名称下；
下一次运行先把旧 MV 原子换回线上目标，再重试新定义，避免过早删除最后一个完整
版本。

已有 MV 的结构变化不会先删除线上对象。Adapter 先创建临时 MV；Immediate 在
默认 `wait_for_refresh=true` 时等待新定义的首次构建成功，再用 Doris 原子
Swap 暴露新定义；Deferred 按其语义不发起首次构建。
残留的 `__dbt_tmp` 或 `__dbt_backup` 对象会在后续运行中按部署状态恢复或清理。

恢复边界：

- 首次创建失败时没有旧版本可恢复，但失败的临时对象不会被当成成功目标。
- MV → MV 的临时 CREATE 或首次任务失败时，现有线上 MV 不变。
- 原子 Swap 后 Inside Post-hook 失败时，下次运行先恢复旧 MV，再重试新定义。
- Outside Post-hook 在 Complete Marker 写入后执行；它失败时不会自动回滚已经
  部署的 MV。
- Hook 副作用和 Doris Grants/DCL 是非事务性的，不随 MV Swap 一起回滚。

`--full-refresh` 是重新部署 MV 定义，不等于只执行
`REFRESH MATERIALIZED VIEW ... COMPLETE`。它不会把 `refresh_method` 改成
`complete`，也不会覆盖 `build_mode`；配置为 `BUILD DEFERRED` 时仍不会发起或
等待首次构建。

## Alias、自定义 Schema 和类型切换

`alias` 修改 Doris 中的最终对象名，`schema` 选择 dbt Schema；在 Doris Adapter
中，Schema 对应 Doris Database：

```sql
-- 文件名仍然是 models/daily_sales.sql
{{ config(
    materialized='materialized_view',
    alias='mv_daily_sales',
    schema='reporting'
) }}

select ...
```

其他 Model 仍使用逻辑 Model 名：

```sql
select * from {{ ref('daily_sales') }}
```

dbt 会把它解析到真实对象 `mv_daily_sales`。使用 dbt 默认
`generate_schema_name` 时，自定义 Schema 通常会与 Profile 的 Target Schema
拼接，例如 `dbt_dev_reporting`；项目重写该 Macro 后也可以生成
`reporting`。

同一个 Model 可以直接修改 `materialized`：

```text
table ↔ view
table ↔ materialized_view
view  ↔ materialized_view
```

用户不需要先手动 Drop，Adapter 会识别现有 Relation Type 并使用对应 DDL。但
不同方向的失败保证并不完全相同：

- Table/View → MV：先构建临时 MV。只有
  `BUILD IMMEDIATE + wait_for_refresh=true` 时，首次构建失败才不会切换旧对象，
  构建成功后才备份旧对象并暴露 MV；`BUILD DEFERRED` 没有首次 Task，关闭等待
  也不具备这项首次任务失败保护。
- MV → Table：先构建临时 Table，再删除 MV 并把临时 Table 改成目标名；最后的
  类型切换不是 Doris 原子 MV Swap。
- MV → View：当前 View Materialization 会先删除 MV，再创建 View；如果 CREATE
  VIEW 失败，目标名可能暂时不存在。
- MV → MV 定义变化：使用 Doris `REPLACE WITH MATERIALIZED VIEW` 原子 Swap，
  这是当前失败恢复保护最完整的路径。

修改 `alias`、`schema` 或删除 Model 时，dbt 不会自动删除原名称或原 Schema
中的孤立对象；需要用户确认并单独清理。

如果目标位置已经存在一个不是由本 Adapter 部署的 Async MV，因为没有
`dbt-doris` Definition Marker，第一次运行会把它视为定义变化。默认
`on_configuration_change='apply'` 会重建；设置 `continue` 会保留并警告，
设置 `fail` 会拒绝接管。

## Hook

Hook 是可选的用户 SQL，用于在 Model 部署动作前后执行额外操作。下面假设
`deployment_audit` 已经存在：

```sql
{{ config(
    materialized='materialized_view',
    pre_hook="set query_timeout = 300",
    post_hook="insert into deployment_audit values ('daily_sales', current_timestamp())"
) }}

select ...
```

- Pre-hook 可用于设置当前 dbt Connection 的 Session 参数或准备辅助对象。
- Post-hook 可用于写部署审计、更新辅助元数据或执行项目自定义维护 SQL。
- Hook 在 `dbt run` 的部署流程中运行；不会在用户查询 MV 或 Doris 自动刷新时
  运行，因此不能把它当作刷新调度器。
- 普通字符串 Hook 默认是 Inside Hook，只在实际创建、替换、类型切换或 Manual
  Refresh 时执行；Schedule/Commit 的 Skip 和配置变化的 Continue 不执行
  Inside Hook。
- 使用 `before_begin`、`after_commit` 等方式配置的 Outside Hook 在每次 Model
  Run 都会执行，包括 Skip 和 Continue。Outside Pre-hook 在
  `SHOW CREATE MATERIALIZED VIEW` 和定义漂移检查前执行；Outside Post-hook
  在部署结果记录和临时对象清理后执行，延迟保留的 Backup 可能在其后删除。
- Hook 失败会让 Model 失败；Adapter 使用 Pending 标记和临时/备份 Relation
  支持 MV → MV 路径的后续恢复。Hook 已经产生的副作用不是事务性操作，不会随
  MV Swap 自动回滚。

## Persist Docs

Relation 和 Column Description 均可持久化：

```yaml
version: 2

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
`LOAD_PRIV`。Adapter 只管理已有 Principal 的 Relation 权限，不创建 Doris
User 或 Role。

- `grants_mode='replace'`：比较该 Relation 上的直接授权，补齐缺少的授权后再
  回收配置中已删除的授权。
- `grants_mode='additive'`：只增加配置中的授权，不回收已有授权。

Replace 只管理目标 Relation 的直接 Table Privileges，不撤销从 Global、
Catalog、Database 或其他 Role 继承的权限。创建、替换、Manual Refresh、Skip
和 `on_configuration_change='continue'` 路径都会应用 Grants，因此项目级
`+grants` 不会让 MV 编译失败，也不会在重复运行时被忽略。

所有模式都会先用 `SHOW ROLES` 批量验证配置中的 User/Role；MV 在任何创建或
替换 DDL 前完成该预检。不存在的 Principal 会让 Model 失败，且不会暴露
新的 MV 定义或执行部分授权。Doris User 名按大小写精确匹配，Role 和 Host
按 Doris 的大小写规则比较。

执行 dbt 的 Doris 身份必须能执行 `SHOW FRONTENDS`，并具有查询、创建、删除、
修改 MV 和管理目标授权所需的权限。配置 Grants 时还需读取 `SHOW ROLES`
（Doris 要求执行身份具备全局 `GRANT_PRIV`）；若执行身份没有该权限，请不要在
该 Model 上配置由 Adapter 管理的 Grants。

## 当前范围边界

当前实现明确不提供：

- Doris Sync Materialized View（Rollup）管理；
- 指定 Doris 分区的刷新配置；
- 内置的 `dbt run-operation` 手动刷新命令；
- 通过 dbt 暂停、恢复或取消 Doris MV Refresh Task。

定义未变化的 `ON MANUAL` Model 会由普通 `dbt run` 提交整项
`AUTO/COMPLETE` Refresh。用户或外部调度系统仍可直接执行 Doris 原生 SQL，
但 Adapter 不生成指定分区 Refresh。如果需要由 Doris 自动触发，应配置
`ON SCHEDULE` 或 `ON COMMIT`。

## 当前验证状态

当前分支已经通过：

- 233 个 Unit Test；
- 65 个 Doris Functional E2E Test；
- dbt 官方 Materialized View 基础生命周期 Contract Test；
- Table、View、Materialized View 双向切换及
  `Table → Materialized View → View → Table` 连续切换；
- Python 3.10、Python 3.14 和 Distribution Build GitHub CI；
- Wheel、sdist 和 Twine Metadata 检查。

## 排错

- `partition_by` 只接受一个分区标识符或 Doris 支持的分区映射函数；多列或任意
  SQL 片段会在 Adapter 校验阶段失败。
- 单 BE 开发集群应设置 `replication_num=1`；顶层值优先于
  `properties.replication_num`。
- 首次构建或 Manual Refresh 超时时先检查 `tasks('type'='mv')`、Task History
  保留设置和 Doris 返回的 ErrorMsg/LastQueryId。
- `refresh_trigger='commit'` 仅在底表变更满足 Doris ON COMMIT 语义时触发，
  Adapter 不模拟 Commit 调度。
- 不要手动删除或修改 MV Comment 中的 `dbt-doris:` 部署标记；Marker 缺失或
  被修改会被识别为定义变化，并可能在默认 `apply` 策略下触发重建。
