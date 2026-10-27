"""Transactions and sessions: explicit and autocommit-off transactions, errors inside them, several sessions."""

from typing import Any

from snowflake.connector import SnowflakeConnection

from probes import UNFILED, Case, Expected, ProbeContext

T = ["create or replace table txn_t (n int)"]


def _error_mid_transaction(conn: SnowflakeConnection, ctx: ProbeContext, failing: str) -> dict[str, Any]:
    """With autocommit off: insert, a failing statement, another insert, commit. What's kept?"""
    cur = conn.cursor()
    cur.execute("create or replace table txn_t (n int)")
    conn.autocommit(False)
    cur.execute("insert into txn_t values (1)")
    failed = ctx.execute(cur, failing)
    second = ctx.execute(cur, "insert into txn_t values (2)")
    commit = ctx.attempt(conn.commit)
    conn.rollback()
    conn.autocommit(True)
    return {
        "failing": failed,
        "second insert": second,
        "commit": commit,
        "kept": ctx.query(cur, "select n from txn_t order by n"),
    }


def compile_error_mid_transaction(conn: SnowflakeConnection, ctx: ProbeContext) -> dict[str, Any]:
    return _error_mid_transaction(conn, ctx, "select * from txn_no_such_table")


def runtime_error_mid_transaction(conn: SnowflakeConnection, ctx: ProbeContext) -> dict[str, Any]:
    return _error_mid_transaction(conn, ctx, "insert into txn_t values ('x')")


def autocommit_off_rollback(conn: SnowflakeConnection, ctx: ProbeContext) -> Any:
    cur = conn.cursor()
    cur.execute("create or replace table txn_t (n int)")
    conn.autocommit(False)
    cur.execute("insert into txn_t values (1)")
    conn.rollback()
    conn.autocommit(True)
    return ctx.query(cur, "select n from txn_t")


def uncommitted_invisible_to_other_session(conn: SnowflakeConnection, ctx: ProbeContext) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute("create or replace table txn_t (n int)")
    cur.execute("begin")
    cur.execute("insert into txn_t values (1)")
    other = ctx.connect().cursor()
    before = ctx.query(other, "select n from txn_t")
    cur.execute("commit")
    after = ctx.query(other, "select n from txn_t")
    return {"before commit": before, "after commit": after}


def temporary_table_other_session(conn: SnowflakeConnection, ctx: ProbeContext) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute("create or replace temporary table txn_temp (x int)")
    other = ctx.connect().cursor()
    return {
        "own session": ctx.query(cur, "select x from txn_temp"),
        "other session": ctx.query(other, "select x from txn_temp"),
    }


def new_session_current_schema(conn: SnowflakeConnection, ctx: ProbeContext) -> Any:
    return ctx.query(ctx.connect().cursor(), "select current_database(), current_schema()")


CASES = [
    Case(
        id="transactions.begin-commit",
        description="BEGIN, INSERT, COMMIT: status rows and the row is kept",
        setup=T,
        sql=["begin", "insert into txn_t values (1)", "commit", "select n from txn_t"],
    ),
    Case(
        id="transactions.begin-rollback",
        description="BEGIN, INSERT, ROLLBACK: the row is gone",
        setup=T,
        sql=["begin transaction", "insert into txn_t values (1)", "rollback", "select n from txn_t"],
    ),
    Case(
        id="transactions.ddl-commits-open-transaction",
        description="DDL inside an explicit transaction commits it first",
        setup=T,
        sql=[
            "begin",
            "insert into txn_t values (1)",
            "create or replace table txn_other (x int)",
            "rollback",
            "select n from txn_t",
        ],
        expected=Expected(
            [UNFILED],
            "DDL inside an explicit transaction fails: DuckDB can't also write fakesnow's "
            "metadata database in it (HTTP 500 in server mode)",
        ),
    ),
    Case(
        id="transactions.current-transaction",
        description="CURRENT_TRANSACTION is NULL outside a transaction and set inside one",
        sql=["select current_transaction() is null", "begin", "select current_transaction() is not null", "rollback"],
        expected=Expected([UNFILED], "CURRENT_TRANSACTION isn't implemented"),
    ),
    Case(
        id="transactions.autocommit-off-rollback",
        description="With autocommit off, rollback discards an insert",
        run=autocommit_off_rollback,
    ),
    Case(
        id="transactions.compile-error-mid-transaction",
        description="A compilation error inside a transaction: later statements and the commit still apply",
        refs=["#459", "#470"],
        run=compile_error_mid_transaction,
    ),
    Case(
        id="transactions.runtime-error-mid-transaction",
        description="A runtime error inside a transaction: Snowflake commits the other writes",
        refs=["#459", "#470"],
        run=runtime_error_mid_transaction,
        expected=Expected(
            ["#470"],
            "DuckDB aborts the transaction on a runtime error, so fakesnow refuses later statements and the commit",
        ),
    ),
    Case(
        id="transactions.uncommitted-invisible-to-other-session",
        description="Another session doesn't see an uncommitted insert, and does after the commit",
        run=uncommitted_invisible_to_other_session,
    ),
    Case(
        id="transactions.temporary-table-other-session",
        description="A temporary table is visible only to the session that created it",
        run=temporary_table_other_session,
    ),
    Case(
        id="transactions.new-session-current-schema",
        description="A new session opened in the run's schema reports it as current",
        run=new_session_current_schema,
    ),
]
