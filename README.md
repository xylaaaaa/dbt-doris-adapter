# dbt-doris-adapter

An independent, community-maintained dbt adapter for Apache Doris.

This repository is derived from the
[`extension/dbt-doris`](https://github.com/apache/doris/tree/master/extension/dbt-doris)
component of Apache Doris. It is not an official Apache Software Foundation or
dbt Labs release.

## Compatibility

| Component | Supported baseline |
| --- | --- |
| dbt Core | 1.12.x |
| Apache Doris runtime gate | 2.x at 2.1.5 or newer; 3.x except 3.0.0; major version 4 or newer |
| Doris Async MV version-gate unit tests | 2.1.5, 2.1.10, 3.0.1, 3.1.0, and 4.1.2 |
| Python | 3.10 or newer |
| Database protocol | Doris MySQL protocol |

The Python distribution remains named `dbt-doris`, and the adapter type used in
`profiles.yml` remains `doris`.

## Install from source

```shell
git clone https://github.com/xylaaaaa/dbt-doris-adapter.git
cd dbt-doris-adapter
python -m pip install .
```

For adapter development, use an editable install with the test dependencies:

```shell
python -m pip install -r dev-requirements.txt
python -m pip install -e .
```

## Configure a profile

Add an output like this to `~/.dbt/profiles.yml`:

```yaml
your_profile_name:
  target: dev
  outputs:
    dev:
      type: doris
      host: 127.0.0.1
      port: 9030
      username: root
      password: ""
      schema: dbt
      threads: 4
```

## Materializations

The adapter contains Doris implementations for table, view, incremental,
partition, snapshot, seed, and asynchronous materialized-view workflows.
Ephemeral models are compiled by dbt Core.

To manage a Doris asynchronous materialized view, configure a model with
`materialized='materialized_view'`:

```sql
{{ config(
    materialized='materialized_view',
    build_mode='immediate',
    refresh_method='auto',
    refresh_trigger='manual',
    wait_for_refresh=true,
    replication_num='1'
) }}

select order_date, sum(amount) as sales
from {{ ref('orders') }}
group by order_date
```

`build_mode='immediate'` is the default. When `CREATE MATERIALIZED VIEW`
starts the initial build for a new or replacement definition, the adapter waits
for that task before exposing the MV, so downstream dbt models do not observe
an unfinished initial build. Create and replacement do not submit an additional
`REFRESH MATERIALIZED VIEW`; they only wait for the task produced by
`BUILD IMMEDIATE`.

For an unchanged `ON MANUAL` MV, a later `dbt run` submits
`REFRESH MATERIALIZED VIEW ... AUTO|COMPLETE` and waits for its new task by
default. The wait defaults to 300 seconds with one-second polling and can be
tuned with `refresh_wait_timeout` and `refresh_poll_interval`. Set
`wait_for_refresh=false` to submit without polling. Waiting requires Doris
materialized-view task history to remain enabled. The adapter identifies a task
by comparing task IDs before and after submission; concurrent refreshes of the
same MV can therefore be mistaken for the task submitted by dbt.

The supported refresh triggers are `manual`, `schedule`, and `commit`.
Production schedules accept `minute`, `hour`, `day`, or `week`. The adapter
rejects `second` because Doris only enables second-level schedules through a
test-only setting.

Common asynchronous MV settings are:

| Config | Purpose |
| --- | --- |
| `build_mode` | `immediate` (default) builds on create/replace; `deferred` creates without an initial build. |
| `refresh_method` | Refresh scope: `auto` (default) lets Doris select partitions when it can track base-table changes; `complete` always refreshes all partitions. For external tables whose changes Doris cannot detect, use `complete`. |
| `refresh_trigger` | Trigger: `manual` (default), `schedule`, or `commit`. |
| `refresh_schedule` | Schedule mapping with `interval`, `unit`, and optional `start_time`. |
| `wait_for_refresh` | Wait for an initial-build or adapter-submitted manual refresh task; defaults to `true`. |
| `refresh_wait_timeout` / `refresh_poll_interval` | Refresh-task timeout and polling interval in seconds. |
| `duplicate_key` | One key column or a list of key columns for `DUPLICATE KEY`. |
| `partition_by` | One partition identifier or Doris-supported function call, supplied as a string or single-item list. |
| `distribution_type` | `hash` or `random`; inferred from whether `distributed_by` is set. |
| `distributed_by` / `buckets` | Doris distribution columns and bucket count. |
| `replication_num` | Convenience setting merged into `properties`; it takes precedence over the same key in `properties`. |
| `properties` | Additional Doris MV properties as a dictionary. |
| `on_configuration_change` | `apply` (atomic replacement), `continue`, or `fail`. |

dbt-doris manages both MV deployment and the `ON MANUAL` run action. If the
deployed definition is unchanged:

- `ON MANUAL` submits `REFRESH MATERIALIZED VIEW ... AUTO|COMPLETE`;
- `ON SCHEDULE` and `ON COMMIT` skip, leaving refresh timing to Doris.

This also makes `BUILD DEFERRED + ON MANUAL` deterministic: the first
`dbt run` only creates the MV, and the second unchanged run submits its first
refresh.

If the definition changed and `on_configuration_change=continue`, the adapter
keeps the deployed definition and does not submit a manual refresh.

For an initial build or manual refresh that it waits for, the adapter polls
Doris `tasks('type'='mv')`; the dbt adapter response includes the successful
task ID, status, and last query ID when Doris provides one. A failed, canceled,
unexpected, or timed-out task fails the model instead of being reported as a
successful action. A dbt timeout does not cancel the asynchronous task already
submitted to Doris.

Outside-transaction pre-hooks run before deployed-definition inspection.
Definition changes are built as a temporary MV and exposed through Doris's
atomic materialized-view replacement; with `BUILD IMMEDIATE`, exposure happens
only after the initial build succeeds, while `BUILD DEFERRED` intentionally has
no initial build task to wait for. The deployment marker is finalized after
inside-transaction post-hooks, allowing a later run to detect and recover an
interrupted deployment. If an atomic replacement succeeded but an inside
post-hook failed, the previous MV remains under the temporary name; the next
run atomically restores it before retrying the deployment.

`persist_docs` is supported for both the MV relation and its columns. The
relation description is included in the MV comment only when
`persist_docs.relation` is enabled; the adapter's definition/deployment marker
remains in that comment independently. Column descriptions are rendered in the
MV column definitions when `persist_docs.columns` is enabled.

Doris relation grants require explicit principals:

```yaml
models:
  your_project:
    +grants:
      select:
        - "role:analyst"
        - "user:reporter@%"
    +grants_mode: replace
```

Before any materialized-view DDL, the adapter validates every configured
principal so an invalid User or Role cannot expose a new MV definition or leave
partial grants. `grants_mode: replace` converges direct relation grants by
revoking stale entries; `additive` only adds configured privileges. User
identities must use
`user:<name>@<host>` (or `user:<name>@[<domain>]`) and roles must use
`role:<name>`, so the adapter never guesses whether a bare name is a User or a
Role. Managing grants requires an execution identity that can read `SHOW ROLES`
and administer privileges on the target relation.

Asynchronous-MV compatibility is:

| Doris release | Current runtime gate |
| --- | --- |
| 2.x | Version 2.1.5 or newer |
| 3.x | Every version except 3.0.0 |
| 4 and newer major versions | Accepted by the current gate |

Before managing an asynchronous MV, the adapter prefers the connected and
Master FE versions from `SHOW FRONTENDS`; if neither role can be identified, it
validates the first returned row. An unparsable or unsupported selected FE is
rejected. Doris 3.0.0 is excluded because it does not provide the atomic
materialized-view replacement semantics used by this lifecycle. Acceptance by
the runtime gate is not a compatibility guarantee for an untested future Doris
release.

Only Doris asynchronous materialized views are managed. Synchronous
materialized views (rollups) have a different lifecycle and remain explicitly
out of scope.

The complete configuration and lifecycle guide is available in
[docs/materialized-view.zh-CN.md](docs/materialized-view.zh-CN.md). The
[implementation TODO](docs/dbt-doris-todo-list.zh-CN.md) and
[#65967 acceptance requirements](docs/dbt-doris-issue-65967-async-materialized-view-requirements.zh-CN.md)
record the delivered scope and remaining adapter work.

## Test

Unit tests do not require a Doris cluster:

```shell
python -m pytest test/unit
python -m flake8 dbt test
```

Functional tests require a reachable Doris cluster. The defaults target
`127.0.0.1:9030`, user `root`, schema `dbt_test`, and one replica:

```shell
DORIS_TEST_HOST=127.0.0.1 \
DORIS_TEST_PORT=9030 \
DORIS_TEST_USER=root \
DORIS_TEST_PASSWORD='' \
DORIS_TEST_SCHEMA=dbt_test \
DORIS_TEST_REPLICATION_NUM=1 \
  python -m pytest test/functional
```

## License and upstream

The code is licensed under Apache License 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE). The source snapshot and migration boundary are recorded in
[UPSTREAM.md](UPSTREAM.md).
