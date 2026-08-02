#!/usr/bin/env python
# encoding: utf-8

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

"""Adapter-side coverage needed by incremental schema handling."""

import pytest

from dbt.adapters.doris.column import DorisColumn
from dbt.adapters.doris.impl import (
    DorisAdapter,
    _doris_view_query_from_show_create,
    _rewrite_doris_view_ddl,
)
from dbt.adapters.doris.relation import DorisRelation
from dbt.exceptions import DbtRuntimeError

from .macro_harness import MacroRunner


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("VARCHAR(20)", "varchar(20)"),
        ("DECIMAL(18, 4)", "decimal(18,4)"),
        ("CHAR(7)", "CHAR(7)"),
        ("DATETIMEV2(6)", "DATETIMEV2(6)"),
        ("ARRAY<VARCHAR(20)>", "ARRAY<VARCHAR(20)>"),
    ],
)
def test_doris_column_preserves_parameterized_types(raw_type, expected):
    column = DorisColumn.from_description("value", raw_type)
    assert column.data_type == expected


def test_doris_column_widens_with_valid_varchar_syntax():
    target = DorisColumn.from_description("value", "varchar(10)")
    source = DorisColumn.from_description("value", "varchar(40)")

    assert target.can_expand_to(source)
    assert DorisColumn.string_type(source.string_size()) == "varchar(40)"


@pytest.mark.parametrize(
    "show_create_sql",
    [
        (
            "CREATE VIEW `events`\n"
            "(ASSET_ID, VALUE)\n"
            " AS select 1 AS `ASSET_ID`, 'current' AS `VALUE`;"
        ),
        (
            "CREATE VIEW `events`\n"
            "(friendly COMMENT 'render AS label')\n"
            " COMMENT 'view AS comment' AS "
            "select 1 AS `friendly`;"
        ),
        (
            "CREATE VIEW `events`\n"
            "(`order AS label`) /* metadata AS text */\n"
            " AS select 1 AS `order AS label`;"
        ),
    ],
)
def test_view_query_extraction_ignores_as_outside_the_query(show_create_sql):
    expected = show_create_sql.rsplit(" AS select", 1)[1]
    expected = "select" + expected.rstrip(";")

    query = MacroRunner(
        "adapters/relation.sql",
        context={"adapter": object.__new__(DorisAdapter)},
    ).render(
        "doris__view_query_from_show_create",
        show_create_sql,
    )

    assert query == expected
    assert _doris_view_query_from_show_create(show_create_sql) == expected


def test_view_ddl_rewrite_preserves_columns_comments_and_query():
    show_create_sql = (
        "CREATE VIEW `events`\n"
        "(friendly COMMENT 'render AS label')\n"
        " COMMENT 'view AS comment' AS select 1 AS `friendly`;"
    )

    rewritten = _rewrite_doris_view_ddl(
        show_create_sql,
        "`analytics`.`events__dbt_backup`",
    )

    assert rewritten == show_create_sql.replace(
        "`events`",
        "`analytics`.`events__dbt_backup`",
        1,
    )


def test_schema_change_comparison_is_case_insensitive_for_doris_columns():
    source_relation = DorisRelation.create(
        schema="analytics",
        identifier="source",
    )
    target_relation = DorisRelation.create(
        schema="analytics",
        identifier="target",
    )

    class SchemaAdapter:
        @staticmethod
        def get_columns_in_relation(relation):
            if relation.identifier == "source":
                return [
                    DorisColumn.from_description("ID", "INT"),
                    DorisColumn.from_description("VALUE", "VARCHAR(20)"),
                ]
            return [
                DorisColumn.from_description("id", "INT"),
                DorisColumn.from_description("value", "VARCHAR(20)"),
            ]

    changes = MacroRunner(
        "adapters/columns.sql",
        context={"adapter": SchemaAdapter()},
    ).render(
        "doris__check_for_schema_changes",
        source_relation,
        target_relation,
    )

    assert changes["schema_changed"] is False
    assert changes["source_not_in_target"] == []
    assert changes["target_not_in_source"] == []
    assert changes["new_target_types"] == []


def test_string_widening_matches_doris_columns_case_insensitively(monkeypatch):
    adapter = object.__new__(DorisAdapter)
    source_relation = DorisRelation.create(
        schema="analytics",
        identifier="source",
    )
    target_relation = DorisRelation.create(
        schema="analytics",
        identifier="target",
    )

    def columns(relation):
        if relation.identifier == "source":
            return [DorisColumn.from_description("VALUE", "VARCHAR(40)")]
        return [DorisColumn.from_description("value", "VARCHAR(5)")]

    alterations = []
    monkeypatch.setattr(adapter, "get_columns_in_relation", columns)
    monkeypatch.setattr(
        adapter,
        "alter_column_type",
        lambda relation, column_name, new_type: alterations.append(
            (relation, column_name, new_type)
        ),
    )

    adapter.expand_column_types(source_relation, target_relation)

    assert alterations == [(target_relation, "value", "varchar(40)")]


def test_schema_change_waits_for_finished_job(monkeypatch):
    adapter = object.__new__(DorisAdapter)
    relation = DorisRelation.create(schema="analytics", identifier="events")
    jobs = iter(
        [
            {"job_id": "2", "state": "RUNNING", "message": ""},
            {"job_id": "2", "state": "FINISHED", "message": ""},
        ]
    )
    monkeypatch.setattr(adapter, "_latest_schema_change_job", lambda _: next(jobs))
    sleeps = []
    monkeypatch.setattr(
        "dbt.adapters.doris.impl.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    adapter.wait_for_schema_change(relation, previous_job_id="1")

    assert sleeps == [0.2]


def test_schema_change_waits_for_new_job_to_appear(monkeypatch):
    adapter = object.__new__(DorisAdapter)
    relation = DorisRelation.create(schema="analytics", identifier="events")
    jobs = iter(
        [
            {"job_id": "1", "state": "FINISHED", "message": ""},
            {"job_id": "2", "state": "FINISHED", "message": ""},
        ]
    )
    monkeypatch.setattr(adapter, "_latest_schema_change_job", lambda _: next(jobs))
    sleeps = []
    monkeypatch.setattr(
        "dbt.adapters.doris.impl.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    adapter.wait_for_schema_change(relation, previous_job_id="1")

    assert sleeps == [0.2]


def test_latest_schema_change_job_orders_by_job_id(monkeypatch):
    adapter = object.__new__(DorisAdapter)
    relation = DorisRelation.create(schema="analytics", identifier="events")
    captured = {}

    class Result:
        rows = []

    def execute(sql, auto_begin, fetch):
        captured["sql"] = sql
        return None, Result()

    monkeypatch.setattr(adapter, "execute", execute)

    assert adapter._latest_schema_change_job(relation) is None
    assert "order by JobId desc limit 1" in captured["sql"]


def test_cancelled_schema_change_is_reported(monkeypatch):
    adapter = object.__new__(DorisAdapter)
    relation = DorisRelation.create(schema="analytics", identifier="events")
    monkeypatch.setattr(
        adapter,
        "_latest_schema_change_job",
        lambda _: {
            "job_id": "2",
            "state": "CANCELLED",
            "message": "invalid type conversion",
        },
    )

    with pytest.raises(DbtRuntimeError) as excinfo:
        adapter.wait_for_schema_change(relation, previous_job_id="1")

    assert "invalid type conversion" in str(excinfo.value)
