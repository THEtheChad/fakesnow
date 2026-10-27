"""write_pandas: identifier quoting, auto-created tables and the result it returns."""

from typing import Any

import pandas as pd
from snowflake.connector import SnowflakeConnection

from probes import Case, Expected, ProbeContext

OVER_SERVER = Expected(
    ["#493"],
    "over fakesnow's server, the connector's write_pandas fails: its COPY transformation reads from the stage as a "
    "string literal ('invalid URL prefix'), and auto_create_table's INFER_SCHEMA isn't supported",
    targets=["fakesnow-server"],
)


def _columns(conn: SnowflakeConnection, ctx: ProbeContext, table: str) -> Any:
    return ctx.query(
        conn.cursor(),
        "select table_name, column_name, data_type from information_schema.columns "
        f"where table_schema = '{{schema}}' and lower(table_name) = lower('{table}') order by 1, 2",
    )


def lower_case_auto_create(conn: SnowflakeConnection, ctx: ProbeContext) -> dict[str, Any]:
    result = ctx.write_pandas(conn, pd.DataFrame({"id": [1]}), "wp_lower", auto_create_table=True)
    return {"result": result[:3], "columns": _columns(conn, ctx, "wp_lower")}


def lower_column_into_upper_table(conn: SnowflakeConnection, ctx: ProbeContext) -> Any:
    conn.cursor().execute("create or replace table WP_UPPER (ID int)")
    return ctx.write_pandas(conn, pd.DataFrame({"id": [1]}), "WP_UPPER")[:3]


def invalid_unquoted_column(conn: SnowflakeConnection, ctx: ProbeContext) -> Any:
    conn.cursor().execute("create or replace table WP_BAD (ID int)")
    return ctx.write_pandas(conn, pd.DataFrame({"my col": [1]}), "WP_BAD", quote_identifiers=False)[:3]


def into_existing_table(conn: SnowflakeConnection, ctx: ProbeContext) -> dict[str, Any]:
    conn.cursor().execute("create or replace table WP_EXISTING (ID int, NAME varchar)")
    df = pd.DataFrame({"ID": [1, 2], "NAME": ["a", None]})
    result = ctx.write_pandas(conn, df, "WP_EXISTING")
    return {"result": result[:3], "rows": ctx.query(conn.cursor(), "select ID, NAME from WP_EXISTING order by ID")}


def auto_create_types(conn: SnowflakeConnection, ctx: ProbeContext) -> Any:
    df = pd.DataFrame({"I": [1], "F": [1.5], "S": ["x"], "B": [True]})
    ctx.write_pandas(conn, df, "WP_TYPES", auto_create_table=True)
    return _columns(conn, ctx, "WP_TYPES")


CASES = [
    Case(
        id="write-pandas.lower-case-auto-create",
        description="write_pandas auto-creating a table from a lower-case name and column (quoted by default)",
        refs=["#468"],
        run=lower_case_auto_create,
        expected=OVER_SERVER,
    ),
    Case(
        id="write-pandas.lower-column-into-upper-table",
        description="write_pandas of a lower-case column into an upper-case table: quoted, so no match",
        refs=["#468"],
        run=lower_column_into_upper_table,
        expected=OVER_SERVER,
    ),
    Case(
        id="write-pandas.invalid-unquoted-column",
        description="write_pandas with quote_identifiers=False and a column name with a space",
        refs=["#468"],
        run=invalid_unquoted_column,
    ),
    Case(
        id="write-pandas.into-existing-table",
        description="write_pandas into an existing table, with a NULL",
        run=into_existing_table,
        expected=OVER_SERVER,
    ),
    Case(
        id="write-pandas.auto-create-types",
        description="Column types of a table auto-created from int, float, str and bool columns",
        run=auto_create_types,
        expected=OVER_SERVER,
    ),
]
