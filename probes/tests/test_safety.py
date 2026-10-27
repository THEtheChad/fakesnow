"""Ownership, interrupted cleanup, transaction isolation, and secret-safe diagnostics."""

from typing import Any
from unittest.mock import MagicMock

import pytest
import snowflake.connector

from probes import Case, ProbeContext
from probes.runner import run_case
from probes.targets import SNOWFLAKE, Names, SnowflakeConfig, TargetError, _drop_run_objects, open_target


def test_cleanup_only_drops_literal_owned_prefix():
    names = Names("DB", "PROBES_20261026_120000_ABCDEF")
    conn = MagicMock()
    cur = conn.cursor.return_value
    other = "PROBESX20261026X120000XABCDEF_CUSTOMER"
    cur.fetchall.return_value = [(None, names.schema + "_EXTRA"), (None, other)]
    _drop_run_objects(conn, names)
    commands = [call.args[0] for call in cur.execute.call_args_list]
    assert any(f'drop database if exists "{names.schema}_EXTRA"' in sql for sql in commands)
    assert not any(other in sql for sql in commands)


@pytest.mark.parametrize("during_create", [False, True])
def test_snowflake_interrupt_cleans_up_with_independent_connection(
    monkeypatch: pytest.MonkeyPatch, during_create: bool
):
    names = Names("DB", "PROBES_INTERRUPT")
    original, cleanup = MagicMock(), MagicMock()
    original.cursor.return_value.fetchone.return_value = ("version",)
    cleanup.cursor.return_value.fetchall.return_value = []

    def execute(sql: str) -> None:
        if during_create and sql.startswith("create schema"):
            raise KeyboardInterrupt

    original.cursor.return_value.execute.side_effect = execute
    connect = MagicMock(side_effect=[original, cleanup])
    monkeypatch.setattr(snowflake.connector, "connect", connect)
    with pytest.raises(KeyboardInterrupt), open_target(SNOWFLAKE, names, SnowflakeConfig("DB", {})):
        raise KeyboardInterrupt
    cleanup.cursor.return_value.execute.assert_any_call("drop schema if exists DB.PROBES_INTERRUPT cascade")
    original.close.assert_called_once()
    cleanup.close.assert_called_once()


def test_case_interrupt_still_runs_teardown():
    def interrupt(conn: Any, ctx: ProbeContext) -> None:
        raise KeyboardInterrupt

    with open_target("fakesnow", Names.new("DB")) as session:
        case = Case(id="interrupt", description="interrupt", run=interrupt, teardown=["create table cleaned (x int)"])
        with pytest.raises(KeyboardInterrupt):
            run_case(case, session)
        cur = session.conn.cursor()
        cur.execute("select count(*) from cleaned")
        assert cur.fetchall() == [(0,)]


def test_commented_begin_does_not_leak_transaction_to_next_case():
    with open_target("fakesnow", Names.new("DB")) as session:
        case = Case(
            id="transaction",
            description="open transaction",
            setup=["create table t (x int)"],
            sql=["/* comment */ begin", "insert into t values (1)"],
        )
        run_case(case, session)
        cur = session.conn.cursor()
        cur.execute("select * from t")
        assert cur.fetchall() == []


def test_names_reject_sql_fragments():
    with pytest.raises(TargetError, match="identifiers"):
        Names("DB; DROP DATABASE OTHER", "SCHEMA")
