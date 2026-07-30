#!/usr/bin/env python
# encoding: utf-8

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from dbt.adapters.sql import SQLAdapter

from dataclasses import dataclass
from enum import Enum
import re
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

import agate
import dbt.exceptions
from dbt.adapters.base import available
from dbt.adapters.base.relation import BaseRelation
from dbt.adapters.contracts.connection import AdapterResponse
from dbt.adapters.doris.column import DorisColumn
from dbt.adapters.doris.connections import DorisConnectionManager
from dbt.adapters.doris.relation import DorisRelation
from dbt.adapters.protocol import AdapterConfig
from dbt.adapters.contracts.relation import RelationType
from dbt.adapters.sql.impl import LIST_RELATIONS_MACRO_NAME, LIST_SCHEMAS_MACRO_NAME
from dbt_common.clients.agate_helper import table_from_rows
from dbt.adapters.doris.doris_column_item import DorisColumnItem


_DORIS_DEFAULT_ROLE_PREFIX = "default_role_rbac_"
_DORIS_ROLE_PRIVILEGE_TO_DBT = {
    "select_priv": "select",
    "load_priv": "insert",
}
_DORIS_USER_IDENTITY = re.compile(
    r"^'(?P<user>[^']+)'@(?:'(?P<host>[^']+)'|\['(?P<domain>[^']+)'\])$"
)
_DORIS_DBT_USER_PRINCIPAL = re.compile(
    r"^user:(?P<user>[^@]+)@(?P<host>.+)$"
)
_DORIS_VERSION = re.compile(
    r"(?:^|doris-)(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
)


@dataclass
class DorisMaterializedViewAdapterResponse(AdapterResponse):
    """dbt result metadata for a Doris asynchronous MV refresh task."""

    task_id: Optional[str] = None
    task_status: Optional[str] = None
    task_error: Optional[str] = None


def _doris_grantee_from_role_row(role_name: str, users: Optional[str]) -> str:
    """Return the explicit dbt-doris principal represented by a SHOW ROLES row."""
    if not role_name.startswith(_DORIS_DEFAULT_ROLE_PREFIX):
        return f"role:{role_name}"

    match = _DORIS_USER_IDENTITY.fullmatch(users or "")
    if match is None:
        raise dbt.exceptions.DbtRuntimeError(
            "Could not identify the Doris user represented by default role "
            f"{role_name!r}."
        )

    host = match.group("host")
    if host is not None:
        return f"user:{match.group('user')}@{host}"
    return f"user:{match.group('user')}@[{match.group('domain')}]"


def _doris_grantee_key(grantee: str) -> Tuple[str, ...]:
    """Return a key matching Doris principal case-sensitivity rules.

    Doris Role and Host names are case-insensitive, while User names are
    case-sensitive. Domain identities remain distinct from ordinary Host
    identities even when their text is otherwise identical.
    """
    if grantee.startswith("role:") and grantee[5:]:
        return ("role", grantee[5:].casefold())

    match = _DORIS_DBT_USER_PRINCIPAL.fullmatch(grantee)
    if match is not None:
        host = match.group("host")
        is_domain = host.startswith("[") and host.endswith("]")
        if is_domain:
            host = host[1:-1]
        if host:
            return (
                "user",
                match.group("user"),
                "domain" if is_domain else "host",
                host.casefold(),
            )

    raise dbt.exceptions.DbtRuntimeError(
        "Invalid Doris grant principal "
        f"{grantee!r}; expected role:<name> or user:<name>@<host>."
    )


def _diff_doris_grants_dict(
        grants: Dict[str, List[str]],
        reference_grants: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """Return grants absent from a reference using Doris case semantics."""
    reference_keys: Dict[str, Set[Tuple[str, ...]]] = {}
    for privilege, grantees in reference_grants.items():
        normalized_privilege = str(privilege).casefold()
        reference_keys.setdefault(normalized_privilege, set()).update(
            _doris_grantee_key(grantee) for grantee in grantees
        )

    difference: Dict[str, List[str]] = {}
    difference_keys: Dict[str, Set[Tuple[str, ...]]] = {}
    for privilege, grantees in grants.items():
        normalized_privilege = str(privilege).casefold()
        known_keys = reference_keys.get(normalized_privilege, set())
        emitted_keys = difference_keys.setdefault(normalized_privilege, set())
        for grantee in grantees:
            grantee_key = _doris_grantee_key(grantee)
            if grantee_key in known_keys or grantee_key in emitted_keys:
                continue
            difference.setdefault(normalized_privilege, []).append(grantee)
            emitted_keys.add(grantee_key)

    return difference


def _standardize_doris_grants_dict(
        roles_table: agate.Table, relation: BaseRelation
) -> Dict[str, List[str]]:
    """Normalize direct User and Role grants from ``SHOW ROLES``.

    Doris represents privileges granted directly to a User in an internal
    ``default_role_rbac_*`` role. Reading ``information_schema.table_privileges``
    is not sufficient because that view expands inherited Role privileges into
    one row per User. ``SHOW ROLES`` with ``show_user_default_role=true`` keeps
    the two sources distinct and makes revocation safe.
    """
    if relation.schema is None or relation.identifier is None:
        raise dbt.exceptions.DbtRuntimeError(
            "Doris grants require a relation with both schema and identifier."
        )

    target = f"internal.{relation.schema}.{relation.identifier}"
    grants: Dict[str, List[str]] = {}

    for row in roles_table:
        table_privileges = row["TablePrivs"]
        if not table_privileges:
            continue

        for entry in table_privileges.split("; "):
            try:
                object_name, privilege_list = entry.rsplit(": ", 1)
            except ValueError as exc:
                raise dbt.exceptions.DbtRuntimeError(
                    f"Could not parse Doris TablePrivs entry {entry!r}."
                ) from exc

            if object_name != target:
                continue

            managed_privileges = [
                _DORIS_ROLE_PRIVILEGE_TO_DBT[doris_privilege]
                for doris_privilege in (
                    privilege.strip().casefold()
                    for privilege in privilege_list.split(",")
                )
                if doris_privilege in _DORIS_ROLE_PRIVILEGE_TO_DBT
            ]
            if not managed_privileges:
                continue

            grantee = _doris_grantee_from_role_row(row["Name"], row["Users"])
            for dbt_privilege in managed_privileges:
                grants.setdefault(dbt_privilege, []).append(grantee)

    standardized: Dict[str, List[str]] = {}
    for privilege, grantees in grants.items():
        unique_grantees: Dict[Tuple[str, ...], str] = {}
        for grantee in grantees:
            unique_grantees.setdefault(_doris_grantee_key(grantee), grantee)
        standardized[privilege] = sorted(
            unique_grantees.values(),
            key=lambda grantee: (_doris_grantee_key(grantee), grantee),
        )
    return standardized


def _validate_doris_grantees_exist(
        roles_table: agate.Table,
        grant_config: Dict[str, List[str]],
) -> None:
    """Validate all requested principals before mutating non-transactional DCL."""
    existing_grantees = set()
    for row in roles_table:
        role_name = str(row["Name"])
        if not role_name.startswith(_DORIS_DEFAULT_ROLE_PREFIX):
            existing_grantees.add(_doris_grantee_key(f"role:{role_name}"))
            continue

        match = _DORIS_USER_IDENTITY.fullmatch(str(row["Users"] or ""))
        if match is None:
            continue
        host = match.group("host")
        if host is not None:
            grantee = f"user:{match.group('user')}@{host}"
        else:
            grantee = (
                f"user:{match.group('user')}@[{match.group('domain')}]"
            )
        existing_grantees.add(_doris_grantee_key(grantee))

    requested_grantees = {
        grantee
        for grantees in grant_config.values()
        for grantee in grantees
    }
    missing_grantees = sorted(
        (
            grantee
            for grantee in requested_grantees
            if _doris_grantee_key(grantee) not in existing_grantees
        ),
        key=lambda grantee: (grantee.casefold(), grantee),
    )
    if missing_grantees:
        raise dbt.exceptions.DbtRuntimeError(
            "The following Doris grant principals do not exist: "
            f"{', '.join(missing_grantees)}. No privileges were changed."
        )


def _validate_doris_materialized_view_version(
        frontends_table: agate.Table,
) -> None:
    """Reject Doris releases missing the Async MV atomic-replace contract."""
    if "Version" not in frontends_table.column_names or not frontends_table.rows:
        raise dbt.exceptions.DbtRuntimeError(
            "Could not determine the connected Doris FE version from "
            "SHOW FRONTENDS."
        )

    required_rows = [
        row
        for row in frontends_table.rows
        if (
            "CurrentConnected" in frontends_table.column_names
            and str(row["CurrentConnected"]).casefold() in {"yes", "true"}
        )
        or (
            "IsMaster" in frontends_table.column_names
            and str(row["IsMaster"]).casefold() in {"yes", "true"}
        )
    ]
    if not required_rows:
        required_rows = [frontends_table.rows[0]]

    for row in required_rows:
        version_text = str(row["Version"])
        match = _DORIS_VERSION.search(version_text)
        if match is None:
            raise dbt.exceptions.DbtRuntimeError(
                "Could not parse a required Doris FE version "
                f"{version_text!r} from SHOW FRONTENDS."
            )

        version = tuple(
            int(match.group(component))
            for component in ("major", "minor", "patch")
        )
        supported = (
            (version[0] == 2 and version >= (2, 1, 5))
            or (
                version[0] == 3
                and (version[1] >= 1 or version[2] >= 1)
            )
            or version[0] >= 4
        )
        if not supported:
            raise dbt.exceptions.DbtRuntimeError(
                "Doris asynchronous materialized views require Doris 2.1.5+ "
                "within the 2.1 release line, Doris 3.0.1+, Doris 3.1+, or "
                f"Doris 4.x+. Required FE version: {version_text}. Doris "
                "3.0.0 is unsupported because it lacks atomic "
                "materialized-view replacement."
            )


class Engine(str, Enum):
    olap = "olap"
    mysql = "mysql"
    elasticsearch = "elasticsearch"
    hive = "hive"
    iceberg = "iceberg"


class PartitionType(str, Enum):
    list = "LIST"
    range = "RANGE"


@dataclass
class DorisConfig(AdapterConfig):
    """Doris-specific model configuration understood by dbt Core."""

    engine: Optional[str] = None
    duplicate_key: Optional[Union[str, List[str]]] = None
    partition_by: Optional[Union[str, List[str]]] = None
    partition_type: str = PartitionType.range.value
    partition_by_init: Optional[List[str]] = None
    distributed_by: Optional[Union[str, List[str]]] = None
    buckets: Optional[Union[int, str]] = None
    properties: Optional[Dict[str, Any]] = None
    replication_num: Optional[Union[int, str]] = None

    # Doris asynchronous materialized-view configuration.
    build_mode: str = "immediate"
    refresh_method: str = "auto"
    refresh_trigger: str = "manual"
    refresh_schedule: Optional[Dict[str, Any]] = None
    refresh_partitions: Optional[Union[str, List[str]]] = None
    distribution_type: Optional[str] = None
    refresh_on_run: bool = False
    wait_for_refresh: bool = True
    refresh_wait_timeout: int = 300
    refresh_poll_interval: int = 1

    grants_mode: str = "replace"


class DorisAdapter(SQLAdapter):
    ConnectionManager = DorisConnectionManager
    Relation = DorisRelation
    AdapterSpecificConfigs = DorisConfig
    Column = DorisColumn

    @available
    def standardize_doris_grants_dict(
            self, roles_table: agate.Table, relation: BaseRelation
    ) -> Dict[str, List[str]]:
        return _standardize_doris_grants_dict(roles_table, relation)

    @available
    def diff_doris_grants_dict(
            self,
            grants: Dict[str, List[str]],
            reference_grants: Dict[str, List[str]],
    ) -> Dict[str, List[str]]:
        return _diff_doris_grants_dict(grants, reference_grants)

    @available
    def validate_doris_grantees_exist(
            self,
            roles_table: agate.Table,
            grant_config: Dict[str, List[str]],
    ) -> None:
        _validate_doris_grantees_exist(roles_table, grant_config)

    @available
    def materialized_view_adapter_response(
            self,
            action: str,
            relation: BaseRelation,
            refresh_task: Optional[Dict[str, Any]] = None,
    ) -> DorisMaterializedViewAdapterResponse:
        """Build the structured adapter response stored in run_results.json."""
        codes = {
            "create": "CREATE MATERIALIZED VIEW",
            "replace": "REPLACE MATERIALIZED VIEW",
            "replace_type": "CREATE MATERIALIZED VIEW",
            "refresh": "REFRESH MATERIALIZED VIEW",
            "skip": "skip",
            "continue": "skip",
        }
        code = codes[action]
        message = (
            f"skip {relation}"
            if code == "skip"
            else f"{code} {relation}"
        )
        task_id = None
        task_status = None
        task_error = None
        query_id = None
        if refresh_task is not None:
            task_id = str(refresh_task["task_id"])
            task_status = str(refresh_task["status"])
            task_error = str(refresh_task.get("error_message") or "") or None
            query_id = str(refresh_task.get("last_query_id") or "") or None
            message += f"; refresh task {task_id} {task_status}"
            if query_id is not None:
                message += f", query {query_id}"

        return DorisMaterializedViewAdapterResponse(
            _message=message,
            code=code,
            rows_affected=-1,
            query_id=query_id,
            task_id=task_id,
            task_status=task_status,
            task_error=task_error,
        )

    @available
    def validate_materialized_view_version(
            self, frontends_table: agate.Table
    ) -> None:
        _validate_doris_materialized_view_version(frontends_table)

    @classmethod
    def date_function(cls) -> str:
        return "current_date()"

    @classmethod
    def convert_datetime_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        return "datetime"

    @classmethod
    def convert_text_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        return "string"

    @classmethod
    def quote(cls, identifier):
        return "`{}`".format(identifier)

    def check_schema_exists(self, database, schema):
        results = self.execute_macro(LIST_SCHEMAS_MACRO_NAME, kwargs={"database": database})

        exists = True if schema in [row[0] for row in results] else False
        return exists

    def get_relation(self, database: Optional[str], schema: str, identifier: str):
        return super().get_relation(None, schema, identifier)

    def drop_schema(self, relation: BaseRelation):
        relations = self.list_relations(
            database=relation.database,
            schema=relation.schema
        )
        for relation in relations:
            self.drop_relation(relation)
        super().drop_schema(relation)

    def list_relations_without_caching(self, schema_relation: DorisRelation) -> List[DorisRelation]:
        if not self.check_schema_exists(
            schema_relation.database,
            schema_relation.schema,
        ):
            return []

        kwargs = {"schema_relation": schema_relation}
        results = self.execute_macro(LIST_RELATIONS_MACRO_NAME, kwargs=kwargs)

        relations = []
        for row in results:
            if len(row) != 4:
                raise dbt.exceptions.DbtRuntimeError(
                    f"Invalid value from 'show table extended ...', "
                    f"got {len(row)} values, expected 4"
                )
            _database, name, schema, type_info = row
            normalized_type = type_info.lower()
            if normalized_type == RelationType.MaterializedView.value:
                rel_type = RelationType.MaterializedView
            elif normalized_type == RelationType.View.value:
                rel_type = RelationType.View
            else:
                rel_type = RelationType.Table
            relation = self.Relation.create(
                database=None,
                schema=schema,
                identifier=name,
                type=rel_type,
            )
            relations.append(relation)

        return relations

    @classmethod
    def _catalog_filter_table(
            cls, table: agate.Table, used_schemas: FrozenSet[Tuple[str, str]]
    ) -> agate.Table:
        table = table_from_rows(
            table.rows,
            table.column_names,
            text_only_columns=[
                "table_database",
                "table_schema",
                "table_name",
                "table_type",
                "table_comment",
                "table_owner",
                "column_name",
                "column_type",
                "column_comment",
            ],
        )
        return table.where(cls._catalog_filter_schemas(used_schemas))

    @staticmethod
    def _catalog_filter_schemas(
            used_schemas: FrozenSet[Tuple[str, str]]
    ):
        schemas = frozenset(((d or ""), s.lower()) for d, s in used_schemas)

        def predicate(row: agate.Row) -> bool:
            table_database = row.get("table_database") or ""
            table_schema = row.get("table_schema")
            if table_schema is None:
                return False
            return (table_database, table_schema.lower()) in schemas

        return predicate

    def get_filtered_catalog(self, relation_configs, used_schemas, relations=None):
        """Match dbt's empty database name to Doris' single namespace.

        ``DorisRelation`` normalizes database to ``None`` because Doris has no
        catalog level between a connection and a database/schema. Manifest
        nodes, however, carry ``database=''``. dbt Core's selected-relation
        filter treats those as different keys and removes every catalog row.
        Apply the same filter with both representations normalized to the empty
        string, which is also the value returned by ``doris__get_catalog``.
        """
        catalogs, exceptions = super().get_filtered_catalog(
            relation_configs,
            used_schemas,
            relations=None,
        )
        if relations and catalogs:
            relation_map = {
                (
                    (relation.database or "").casefold(),
                    relation.schema.casefold() if relation.schema else None,
                    relation.identifier.casefold() if relation.identifier else None,
                )
                for relation in relations
            }

            def in_map(row):
                database = (row.get("table_database") or "").casefold()
                schema = row.get("table_schema")
                identifier = row.get("table_name")
                schema = schema.casefold() if schema else None
                identifier = identifier.casefold() if identifier else None
                return (database, schema, identifier) in relation_map

            catalogs = catalogs.where(in_map)

        return catalogs, exceptions

    @classmethod
    def convert_number_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        decimals = agate_table.aggregate(agate.HasNulls(col_idx))
        return "double" if decimals else "bigint"

    @classmethod
    def convert_boolean_type(cls, agate_table: agate.Table, col_idx: int) -> str:
        return "boolean"

    def quote_seed_column(self, column: str, quote_config: Optional[bool]) -> str:
        if quote_config is None or quote_config:
            return self.quote(column)
        return column

    # Methods used in adapter tests
    def timestamp_add_sql(self, add_to: str, number: int = 1, interval: str = "hour") -> str:
        # for backwards compatibility, we're compelled to set some sort of
        # default. A lot of searching has lead me to believe that the
        # '+ interval' syntax used in postgres/redshift is relatively common
        # and might even be the SQL standard's intention.
        return f"{add_to} + interval {number} {interval}"

    @classmethod
    def render_raw_columns_constraints(cls, raw_columns: Dict[str, Dict[str, Any]]) -> List:
        rendered_column_constraints = []
        for v in raw_columns.values():
            # DorisColumnItem quotes identifiers when it renders SQL. Passing an
            # already quoted name for `quote: true` produced invalid double
            # backticks such as ``order`` in contracted model projections.
            cols_name = v["name"]
            data_type = v.get('data_type')
            comment = v.get('description')

            column = DorisColumnItem(cols_name, data_type, comment, "")
            rendered_column_constraints.append(column)

        return rendered_column_constraints
