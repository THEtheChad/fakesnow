"""The connector's API: async queries (the monitoring endpoint), describe, rowcount, bindings, pandas."""

import time
from typing import Any

from snowflake.connector import DictCursor, SnowflakeConnection

from probes import UNFILED, Case, Compare, Expected, ProbeContext
from probes.cases._gaps import DUPLICATE_NAMES


def execute_async(conn: SnowflakeConnection, ctx: ProbeContext) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute_async("select 1 as x")
    sfqid = cur.sfqid
    assert sfqid
    deadline = time.monotonic() + 30
    while conn.is_still_running(conn.get_query_status(sfqid)) and time.monotonic() < deadline:
        time.sleep(0.2)
    status = conn.get_query_status(sfqid)
    cur.get_results_from_sfqid(sfqid)
    return {"status": status.name, "rows": cur.fetchall()}


def describe(conn: SnowflakeConnection, ctx: ProbeContext) -> list[tuple[Any, ...]]:
    described = conn.cursor().describe("select 1 as a, 'x' as b, 1.5::float as c, current_date() as d")
    return [(d.name, d.type_code) for d in described or []]


def rowcount(conn: SnowflakeConnection, ctx: ProbeContext) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute("create or replace table conn_rc (x int)")
    counts = {}
    for sql in [
        "insert into conn_rc values (1), (2), (3)",
        "update conn_rc set x = 0 where x > 1",
        "select * from conn_rc",
    ]:
        cur.execute(sql)
        counts[sql] = cur.rowcount
    return counts


def executemany(conn: SnowflakeConnection, ctx: ProbeContext) -> Any:
    cur = conn.cursor()
    cur.execute("create or replace table conn_many (x int, y varchar)")
    cur.executemany("insert into conn_many values (%s, %s)", [(1, "a"), (2, None)])
    return ctx.query(cur, "select x, y from conn_many order by x")


def pyformat_bindings(conn: SnowflakeConnection, ctx: ProbeContext) -> list[tuple[Any, ...]]:
    cur = conn.cursor()
    cur.execute("select %s, %s, %s", (1, "it's", None))
    return cur.fetchall()


def dict_cursor(conn: SnowflakeConnection, ctx: ProbeContext) -> Any:
    cur = conn.cursor(DictCursor)
    cur.execute('select 1 as a, 2 as "b"')
    return cur.fetchall()


def fetch_pandas(conn: SnowflakeConnection, ctx: ProbeContext) -> dict[str, Any]:
    cur = conn.cursor()
    cur.execute("select 1 as i, 1.5 as d, 'x' as s, 1.5::float as f, true as b")
    df = cur.fetch_pandas_all()
    return {"columns": list(df.columns), "dtypes": [str(t) for t in df.dtypes], "rows": df.to_numpy().tolist()}


def last_query_id(conn: SnowflakeConnection, ctx: ProbeContext) -> bool:
    cur = conn.cursor()
    cur.execute("select 1")
    sfqid = cur.sfqid
    cur.execute("select last_query_id()")
    row = cur.fetchone()
    return bool(row) and row[0] == sfqid


CASES = [
    Case(
        id="connector.duplicate-column-names",
        description="A result with two columns of the same name keeps both",
        compare=Compare(names=True),
        sql=["select 1 as a, 2 as a", "select null::timestamp_ltz, null::timestamp_tz"],
        expected=DUPLICATE_NAMES,
    ),
    Case(
        id="connector.execute-async",
        description="execute_async, polling get_query_status (the monitoring endpoint), get_results_from_sfqid",
        run=execute_async,
        expected=Expected(
            [UNFILED], "the in-process connection has no get_query_status or is_still_running", targets=["fakesnow"]
        ),
    ),
    Case(id="connector.describe", description="cursor.describe's names and type codes", run=describe),
    Case(id="connector.rowcount", description="cursor.rowcount after INSERT, UPDATE and SELECT", run=rowcount),
    Case(id="connector.executemany", description="executemany with pyformat parameters", run=executemany),
    Case(
        id="connector.pyformat-bindings",
        description="Client-side pyformat bindings of an int, a string with a quote, and None",
        run=pyformat_bindings,
    ),
    Case(id="connector.dict-cursor", description="DictCursor keys for unquoted and quoted aliases", run=dict_cursor),
    Case(
        id="connector.fetch-pandas-all",
        description="fetch_pandas_all: column names, dtypes and values",
        run=fetch_pandas,
        expected=Expected(
            [UNFILED],
            "fetch_pandas_all gives Decimal objects for a NUMBER(2,1) column and int32 for "
            "NUMBER(1,0); Snowflake float64 and int8",
        ),
    ),
    Case(
        id="connector.last-query-id",
        description="LAST_QUERY_ID() is the previous statement's sfqid",
        run=last_query_id,
        expected=Expected([UNFILED], "LAST_QUERY_ID isn't implemented"),
    ),
]
