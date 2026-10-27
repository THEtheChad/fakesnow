"""Stages: CREATE STAGE with quoted names, LIST, PUT."""

import tempfile
from pathlib import Path
from typing import Any

from snowflake.connector import SnowflakeConnection

from probes import UNFILED, Case, ErrorInfo, Expected, ProbeContext, ResultSet


def _put(conn: SnowflakeConnection, ctx: ProbeContext) -> ResultSet | ErrorInfo:
    conn.cursor().execute("create or replace stage stg_put")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.csv"
        path.write_text("1,a\n2,b\n")
        return ctx.query(conn.cursor(), f"put 'file://{path.as_posix()}' @stg_put auto_compress = false")


def put(conn: SnowflakeConnection, ctx: ProbeContext) -> Any:
    """PUT's result, without target_size: Snowflake's counts the encryption padding."""
    result = _put(conn, ctx)
    if isinstance(result, ErrorInfo):
        return result
    keep = [i for i, c in enumerate(result.columns) if c.name != "target_size"]
    columns = [(result.columns[i].name, result.columns[i].type_name) for i in keep]
    return {"columns": columns, "rows": [tuple(row[i] for i in keep) for row in result.rows]}


def list_after_put(conn: SnowflakeConnection, ctx: ProbeContext) -> Any:
    _put(conn, ctx)
    listed = ctx.query(conn.cursor(), "list @stg_put")
    return [row[0] for row in listed.rows] if isinstance(listed, ResultSet) else listed


def select_from_stage(conn: SnowflakeConnection, ctx: ProbeContext) -> Any:
    _put(conn, ctx)
    return ctx.query(conn.cursor(), "select $1, $2 from @stg_put/probe.csv order by 1")


CASES = [
    Case(
        id="stages.create-quoted-apostrophe",
        description="CREATE STAGE with a quoted name containing a single quote",
        refs=["#467"],
        sql="""create or replace stage "o'brien stage" """,
    ),
    Case(
        id="stages.create-if-not-exists-again",
        description="CREATE STAGE IF NOT EXISTS on an existing quoted stage",
        refs=["#467"],
        setup=["""create or replace stage "o'brien stage" """],
        sql="""create stage if not exists "o'brien stage" """,
    ),
    Case(
        id="stages.list-missing-quoted",
        description="LIST of a quoted stage that doesn't exist, with a name that looks like an injection",
        refs=["#467"],
        sql="""list '@"x'' OR ''1''=''1"'""",
    ),
    Case(
        id="stages.list-empty",
        description="LIST of an empty stage: no rows, and the result's columns",
        setup=["create or replace stage stg_empty"],
        sql="list @stg_empty",
    ),
    Case(
        id="stages.drop-missing",
        description="DROP STAGE of a stage that doesn't exist",
        sql="drop stage stg_no_such_stage",
        expected=Expected([UNFILED], "DROP STAGE isn't supported: parser error"),
    ),
    Case(
        id="stages.put",
        description="PUT a small CSV file: the result's columns and row",
        run=put,
    ),
    Case(id="stages.list-after-put", description="LIST a stage after a PUT", run=list_after_put),
    Case(
        id="stages.select-from-stage",
        description="Select a staged CSV file's columns as $1, $2",
        run=select_from_stage,
        expected=Expected([UNFILED], "selecting from a staged file isn't supported: parser error at '@'"),
    ),
]
