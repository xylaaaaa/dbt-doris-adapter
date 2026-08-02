# dbt-doris Incremental 测试方案

## 1. 目标

本方案验证 dbt-doris 的 Incremental 行为与 Doris 写入语义一致，重点回答：

1. `append`、`merge`、`insert_overwrite` 是否生成正确的 Doris DML；
2. 普通增量是否只使用逻辑临时 View，而不会把同一批数据先写入物理临时表；
3. 必须冻结批次的 Schema Change、自定义策略是否按设计使用物理 staging；
4. 失败、重试、Full Refresh、Relation 类型切换是否保护已有目标数据；
5. 已移除或危险的旧配置是否在 Hook 和数据写入前失败。

这里的“物理写入次数”以 dbt-doris 向 Doris 提交的数据写入语句为边界。
Doris 在 `INSERT OVERWRITE` 内部创建临时分区、写 Rowset 或发布版本，属于存储
引擎实现，不计为 dbt-doris 的第二次物化。

## 2. 测试基线与矩阵

### 2.1 每次提交必须执行

| 层级 | 基线 | 用途 |
| --- | --- | --- |
| Python | 3.10+ | Adapter 与 Unit Test |
| dbt Core | 1.12.x | Strategy Dispatch、Materialization、Schema Change 契约 |
| Doris | 4.1.2-rc01 | 当前真实集群回归基线 |

### 2.2 发布前兼容性矩阵

若发布说明继续声明支持 Doris 2.1+，发布候选版本还应在以下最新补丁版本执行
同一套 Functional Test：

| Doris 系列 | 必测能力 |
| --- | --- |
| 2.1.x | `append`、Unique Key `merge`、MOW/MOR、Sequence、整表/静态分区覆盖 |
| 3.0.x / 3.1.x | 上述能力、动态分区覆盖、Schema Change |
| 4.1.x | 全量用例，以及未来原生 `MERGE INTO` 的独立版本门禁 |

如果某个版本不支持某项 Doris 原生能力，应增加明确的版本门禁和错误消息，
不能通过跳过测试来暗示支持。

## 3. 分层测试

### 3.1 Unit 与宏测试

Unit Test 不依赖 Doris，负责尽早发现：

- 策略允许列表与默认路由错误；
- Jinja 语法、宏 dispatch、参数契约错误；
- 一个策略宏意外生成多条 SQL；
- Key、Partition、Relation 名称未正确引用；
- Schema 类型、大小写匹配和异步 Alter Job 等 Adapter 逻辑错误；
- `SHOW CREATE VIEW` 解析或失败恢复时丢失列名、列注释、View 注释。

执行命令：

```bash
python -m pytest -q test/unit
python -m flake8 dbt test
git diff --check
```

### 3.2 Doris Functional Test

Functional Test 必须连接隔离的 Doris 测试 Schema，并捕获 dbt `SQLQuery`
事件。目录查询只能证明临时对象最终被清理；SQL 事件用于证明运行过程中是否
创建过物理 staging、执行过几条目标 DML。

```bash
DORIS_TEST_HOST=127.0.0.1 \
DORIS_TEST_PORT=9030 \
DORIS_TEST_USER=root \
DORIS_TEST_PASSWORD='' \
DORIS_TEST_SCHEMA=dbt_incremental_ci \
python -m pytest -q test/functional/adapter/test_doris_incremental.py
```

共享 Relation/DDL 宏发生变化时追加：

```bash
python -m pytest -q \
  test/functional/adapter/test_doris_table.py \
  test/functional/adapter/test_doris_view.py \
  test/functional/adapter/test_doris_partition.py
```

### 3.3 Package Test

```bash
python -m build --no-isolation
python -m twine check dist/*
python -m pip check
```

Wheel 中必须包含以下三个文件：

- `materializations/incremental/incremental.sql`
- `materializations/incremental/help.sql`
- `materializations/incremental/strategies.sql`

## 4. “不物理双写”的核心验收

### 4.1 已有目标表的普通内置策略

前置条件：目标表已存在，使用内置策略，且
`on_schema_change='ignore'`。

对 `append`、`merge`、`insert_overwrite` 分别运行第二次 dbt model，捕获该
节点的全部 SQL，并同时满足：

1. 恰好出现一次 `CREATE OR REPLACE VIEW ...__dbt_tmp AS ...`；
2. 不出现 `CREATE TABLE ...__dbt_tmp`；
3. 恰好出现一条写目标表的最终 DML：
   - `append`：一条 `INSERT INTO`；
   - `merge`：一条 `INSERT INTO`，由 Unique Key 完成 Upsert；
   - `insert_overwrite`：一条 `INSERT OVERWRITE`；
4. 不出现 `DELETE FROM`，也不通过 `BEGIN` 包装多语句删除与插入；
5. 运行结束后，`information_schema.tables` 中不存在同模型的
   `__dbt_tmp`、`__dbt_backup` 等辅助 Relation；
6. 最终数据符合各策略语义。

逻辑 View 只保存查询定义。创建 View 是一次元数据 DDL，不会执行模型查询，
因此不算一次数据物化。

### 4.2 首次运行

首次运行应直接执行一次目标表 CTAS：

- 不创建逻辑 View；
- 不创建物理 staging；
- `merge` 的 Key 列按 `unique_key` 配置顺序成为物理 Schema 前缀；
- Source Key 重复时，目标表不能发布部分数据。

### 4.3 Full Refresh

Full Refresh 允许创建 physical intermediate table，但应满足：

- 模型数据只写入 intermediate table 一次；
- 后续通过 `REPLACE WITH TABLE` 或 Rename 做元数据切换；
- 不再执行一次从 intermediate 到最终表的 `INSERT`；
- 新对象准备好之前，旧目标保持可查询；
- 成功后清理旧对象，失败重试时保留或恢复唯一的好副本。

### 4.4 允许物理 staging 的例外

以下场景有意把 Source 批次写入 physical staging，再写目标表：

- `on_schema_change` 为 `fail`、`append_new_columns` 或
  `sync_all_columns`；
- 自定义 Incremental Strategy。

Schema DDL 会改变 Source 与 Target 的可写列集合。冻结批次可避免两次读取
得到不同数据，并让自定义策略继续使用 dbt 标准 `temp_relation` 参数。

验收时必须确认：

- staging 继承必要的 Distribution，以及 `replication_num` 或
  `replication_allocation` 中的一项；
- `fail` 在修改目标 Schema 或数据前失败；
- `append_new_columns`、`sync_all_columns` 等待 Doris Alter Job 完成后才写入；
- 成功后立即清理 staging；失败可能遗留辅助对象，但下一次运行必须先清理或
  替换旧对象，且绝不能读取上一次未完成的批次；
- 测试报告明确把这类两次物理写入标记为设计内行为，不能与普通增量混为一谈。

## 5. 功能用例矩阵

| 编号 | 场景 | 关键断言 | 当前自动化 |
| --- | --- | --- | --- |
| INC-001 | 默认策略，无 `unique_key` | 路由到 `append` | Unit 已覆盖，E2E 待补 |
| INC-002 | 默认策略，有 `unique_key` | 路由到 `merge` | Unit 已覆盖，E2E 待补 |
| INC-010 | `append` 首次与二次运行 | Duplicate Key；旧行保留，新行追加；普通运行无物理 staging | 已覆盖 |
| INC-020 | MOW `merge` | 同 Key 更新、新 Key 插入、未出现旧 Key 保留；一条目标 `INSERT` | 已覆盖 |
| INC-021 | MOR `merge` | 与 MOW 相同的结果语义 | 已覆盖 |
| INC-022 | 复合 Key | 所有 Key 共同参与 Upsert 与重复检查 | 已覆盖 |
| INC-023 | Key 不在 Source 首列 | 首次 CTAS 自动调整物理列顺序 | 已覆盖 |
| INC-024 | Key 是保留字 | Unique Key 与 Distribution 正确引用 | 已覆盖 |
| INC-025 | 批内重复 Key | 同一条 DML 原子失败，目标数据不变 | 已覆盖 |
| INC-026 | 可见 Sequence 列 | 后到达的低 Sequence 不覆盖高 Sequence 行 | 已覆盖 |
| INC-027 | 隐藏 Sequence Type | 写入前拒绝 `function_column.sequence_type` | Unit 已覆盖 |
| INC-030 | 整表 `insert_overwrite` | 本批缺失的旧行被删除；无物理 staging | 已覆盖 |
| INC-031 | 静态分区覆盖 | 只替换命名分区，其他分区不变 | 已覆盖 |
| INC-032 | `PARTITION(*)` | 只动态替换本批涉及的分区 | 已覆盖 |
| INC-040 | `delete+insert` / `delete_insert` | Hook 与 SQL 写入前拒绝；目标 Relation 不存在或数据不变 | 已覆盖 |
| INC-041 | `insert_overwrite + unique_key` | 写入前拒绝并提示迁移到 `merge` 或删除 Key | 已覆盖 |
| INC-042 | `merge` 无 Key | 编译失败并给出配置示例 | Unit 已覆盖 |
| INC-043 | 不支持的 Predicate/部分列 Merge | 写入前提示需要未来原生 `MERGE INTO` | Unit 已覆盖 |
| INC-050 | `ignore` 下 VARCHAR 扩容 | 大小写不敏感匹配；无物理 staging；等待 Alter 完成 | 已覆盖 |
| INC-051 | Key/Sequence 类型变化 | 修改物理不可变列前失败，提示 Full Refresh | Key E2E、Key/Sequence Unit 已覆盖 |
| INC-052 | 仅列名大小写变化 | 不误发 Add + Drop，不删除 Key | 已覆盖 |
| INC-053 | `fail` | 目标 Schema 与数据均不改变 | dbt Core 契约已覆盖；目标不变断言待加强 |
| INC-054 | `append_new_columns` | 新列添加完成后写入冻结批次 | dbt Core 契约已覆盖 |
| INC-055 | `sync_all_columns` | Add/Drop/Type Change 后正确写入 | dbt Core 契约已覆盖 |
| INC-060 | Full Refresh | 配置保留；一次 intermediate CTAS、零 copy INSERT、一次元数据交换 | 已覆盖 |
| INC-061 | View → Table | 保留备份 DDL；安全替换；陈旧 cache 不冲突 | 已覆盖 |
| INC-062 | 失败后仅剩 View 备份 | 下次运行先恢复完整列名和注释，再处理模型 | 已覆盖 |
| INC-063 | 陈旧 temp/intermediate/backup | 开始时清理数据库对象和 Relation cache | View backup E2E 已覆盖，其余待补 |
| INC-070 | 无效 Grants | Principal/Mode 校验先于目标 DML，目标数据不变 | 已覆盖 |
| INC-071 | Pre/Post Hook 失败 | 明确失败发生阶段与目标、辅助对象状态 | 待补 |
| INC-080 | 自定义策略 | physical staging + dbt 标准五参数契约 | 已覆盖 |

## 6. 数据与失败注入

每个策略至少准备以下数据：

- 首批已有 Key、第二批更新 Key、新增 Key、第二批缺失 Key；
- 两列复合 Key，包含不同 Tenant 下相同业务 ID；
- 两行重复 Source Key；
- 高、低两个 Sequence 值；
- 两个静态分区，以及只触达一个分区的增量批次；
- `VARCHAR(5)` 目标与 `VARCHAR(40)` Source；
- 列名只改变大小写的 Source；
- 名称包含保留字、注释包含 ` AS ` 的 Relation 元数据。

失败注入至少覆盖：

- 缺失 Source Relation；
- 重复 Key；
- 不存在的 Grant Role/User；
- Doris Schema Change Job `CANCELLED` 与超时；
- View → Table 第二次 Rename 失败后的重试恢复；
- 无效策略和危险迁移配置。

每个失败用例都必须比较运行前后的目标数据，并检查辅助 Relation。只断言 dbt
返回失败不够，因为 Doris 的 DDL、DML 和 DCL 不由一个 dbt 事务统一回滚。

## 7. 退出标准

合并或发布前必须满足：

1. Unit、Incremental Functional、受影响 Materialization 回归全部通过；
2. 三个普通内置策略均有 SQL 事件证据证明不存在 physical staging；
3. 失败用例证明目标数据不发生部分更新；
4. 测试 Schema 与辅助 Relation 全部清理；
5. wheel/sdist 可构建，宏文件进入 wheel，`twine check` 与 `pip check` 通过；
6. 不新增 warning；现有 Pytest class-scope fixture deprecation warning 应在升级
   Pytest 10 前清理；
7. 测试报告记录 dbt、Python、Doris 精确版本与提交 SHA。

## 8. 当前执行结果

截至 2026-08-02，基于实现提交 `aeacde2` 的当前工作树结果：

| 套件 | 结果 |
| --- | --- |
| Unit Test | 292 passed |
| Doris Incremental Functional | 25 passed |
| Table/View/Partition 受影响回归 | 12 passed |
| Flake8 / `git diff --check` | passed |
| wheel + sdist / Twine / Pip Check | passed |

本轮环境为 Python `3.12.13`、dbt Core `1.12.0` 和
`doris-4.1.2-rc01-4536b29f712`。上述结果证明当前主路径已经通过测试；第 5 节
标记“待补”的项目是进一步扩大失败注入、默认路由 E2E 和跨 Doris 版本覆盖，
不应被误写成已经执行。
