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
| Apache Doris | 2.1.5 or newer |
| Doris Async MV | 2.1.5+ on 2.1.x; 3.0.1+; 3.1.x; 4.x |
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
an unfinished initial build. This is the only materialized-view task that the
normal model lifecycle waits for. The wait defaults to 300 seconds with one-second
polling and can be tuned with `refresh_wait_timeout` and
`refresh_poll_interval`. Set `wait_for_refresh=false` only when asynchronous
completion is intentional. Waiting requires Doris materialized-view task
history to remain enabled so the adapter can identify the initial build task.

The supported refresh triggers are `manual`, `schedule`, and `commit`.
Production schedules accept `minute`, `hour`, `day`, or `week`. The adapter
rejects `second` because Doris only enables second-level schedules through a
test-only setting.

Common asynchronous MV settings are:

| Config | Purpose |
| --- | --- |
| `build_mode` | `immediate` (default) waits for the initial build; `deferred` creates without an initial refresh. |
| `refresh_method` | Doris `auto` (default) or `complete` method written into the MV DDL. |
| `refresh_trigger` | `manual` (default), `schedule`, or `commit`, written into the MV DDL. |
| `refresh_schedule` | Schedule mapping with `interval`, `unit`, and optional `start_time`. |
| `wait_for_refresh` | Wait for the `BUILD IMMEDIATE` initial build task; defaults to `true`. |
| `refresh_wait_timeout` / `refresh_poll_interval` | Initial-build task timeout and polling interval in seconds. |
| `duplicate_key` | One key column or a list of key columns for `DUPLICATE KEY`. |
| `partition_by` | One partition identifier or Doris-supported function call, supplied as a string or single-item list. |
| `distribution_type` | `hash` or `random`; inferred from whether `distributed_by` is set. |
| `distributed_by` / `buckets` | Doris distribution columns and bucket count. |
| `replication_num` | Convenience setting merged into `properties`; it takes precedence over the same key in `properties`. |
| `properties` | Additional Doris MV properties as a dictionary. |
| `on_configuration_change` | `apply` (atomic replacement), `continue`, or `fail`. |

dbt-doris treats a materialized-view model run as definition and configuration
deployment. It manages `CREATE`, atomic replacement, `DROP`, and the refresh
policy in the MV DDL. When the deployed definition is unchanged, the run skips
the MV and does not submit `REFRESH MATERIALIZED VIEW`. Doris owns all
subsequent refresh timing, execution, and partition selection according to that
DDL.

When waiting for a `BUILD IMMEDIATE` initial build, the adapter polls Doris
`tasks('type'='mv')`; the dbt adapter response includes the successful task ID,
status, and last query ID when Doris provides one. A failed, canceled,
unexpected, or timed-out initial build fails the model instead of being
reported as a successful deployment.

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

| Doris release line | Supported versions |
| --- | --- |
| 2.1 | 2.1.5 and newer 2.1.x releases, including `ON COMMIT` |
| 3.0 | 3.0.1 and newer; 3.0.0 is excluded |
| 3.1 | 3.1.x |
| 4.x | 4.x |

Before managing an asynchronous MV, the adapter reads the connected and Master
FE versions from `SHOW FRONTENDS` and rejects an unknown, unparsable, or
unsupported required FE. Doris 3.0.0 is excluded because it does not provide
the atomic materialized-view replacement semantics used by this lifecycle.

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
