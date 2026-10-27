"""DDL and metadata: CREATE/DROP status, quoted names, temporary tables, INFORMATION_SCHEMA, SHOW and DESCRIBE."""

from typing import Any

from snowflake.connector import DictCursor, SnowflakeConnection

from probes import UNFILED, Case, Expected, ProbeContext

INFO = [
    "create or replace table ddl_info (id int not null, name varchar(10), amount number(10,2), "
    "flag boolean, created timestamp_ntz) comment = 'a probe table'",
]


def show(conn: SnowflakeConnection, ctx: ProbeContext, sql: str, keys: list[str]) -> list[tuple[Any, ...]]:
    """Selected columns of a SHOW's rows: the rest (created_on, owner, ...) differ by nature."""
    cur = conn.cursor(DictCursor)
    cur.execute(ctx.sql(sql))
    return [tuple(row[k] for k in keys) for row in cur.fetchall()]


def show_tables(conn: SnowflakeConnection, ctx: ProbeContext) -> list[tuple[Any, ...]]:
    return show(conn, ctx, "show tables like 'DDL_INFO'", ["name", "database_name", "schema_name", "kind", "comment"])


def show_tables_columns(conn: SnowflakeConnection, ctx: ProbeContext) -> list[str]:
    cur = conn.cursor()
    cur.execute("show tables like 'NOTHING_MATCHES'")
    return [d.name for d in cur.description]


def show_columns(conn: SnowflakeConnection, ctx: ProbeContext) -> list[tuple[Any, ...]]:
    return show(
        conn, ctx, "show columns in table ddl_info", ["table_name", "column_name", "data_type", "null?", "kind"]
    )


def show_schemas(conn: SnowflakeConnection, ctx: ProbeContext) -> list[tuple[Any, ...]]:
    return show(conn, ctx, "show schemas like '{schema}' in database {database}", ["name", "database_name"])


def describe_table(conn: SnowflakeConnection, ctx: ProbeContext) -> list[tuple[Any, ...]]:
    return show(conn, ctx, "describe table ddl_info", ["name", "type", "kind", "null?", "primary key"])


CASES = [
    # --- statements and their status rows
    Case(
        id="ddl.create-table-status",
        description="CREATE TABLE, CREATE TABLE IF NOT EXISTS on an existing table, and DROP TABLE status rows",
        sql=[
            "create table ddl_s (x int)",
            "create table if not exists ddl_s (x int)",
            "create or replace table ddl_s (x int)",
            "drop table ddl_s",
            "drop table if exists ddl_s",
        ],
        expected=Expected(
            [UNFILED],
            "the status rows of CREATE TABLE IF NOT EXISTS on an existing table and DROP TABLE IF "
            "EXISTS on a missing one claim it was created / dropped",
        ),
    ),
    Case(
        id="ddl.create-view-status",
        description="CREATE VIEW and selecting from it",
        setup=["create or replace table ddl_vt (x int)", "insert into ddl_vt values (1)"],
        sql=["create or replace view ddl_view as select x + 1 as y from ddl_vt", "select y from ddl_view"],
    ),
    Case(
        id="ddl.dml-status",
        description="Rows reported by INSERT, UPDATE and DELETE",
        setup=["create or replace table ddl_dml (x int)"],
        sql=[
            "insert into ddl_dml values (1), (2), (3)",
            "update ddl_dml set x = x + 10 where x > 1",
            "delete from ddl_dml where x = 1",
            "insert into ddl_dml select x from ddl_dml",
        ],
    ),
    Case(
        id="ddl.alter-table",
        description="ALTER TABLE ADD COLUMN and RENAME COLUMN",
        setup=["create or replace table ddl_alt (x int)", "insert into ddl_alt values (1)"],
        sql=[
            "alter table ddl_alt add column y varchar",
            "alter table ddl_alt rename column x to z",
            "select z, y from ddl_alt",
        ],
    ),
    Case(
        id="ddl.ctas-types",
        description="CREATE TABLE AS SELECT sizes the column types from the query",
        setup=["create or replace table ddl_ctas as select 1 as i, 'ab' as s, 1.5 as d, current_date() as dt"],
        sql="select column_name, data_type, character_maximum_length, numeric_precision, numeric_scale "
        "from information_schema.columns where table_schema = '{schema}' and table_name = 'DDL_CTAS' "
        "order by ordinal_position",
        expected=Expected(
            [UNFILED],
            "CTAS columns from an integer and a string literal are INTEGER and unsized TEXT; "
            "Snowflake sizes them NUMBER(1,0) and VARCHAR(2)",
        ),
    ),
    Case(
        id="ddl.sequence",
        description="A sequence's NEXTVAL",
        setup=["create or replace sequence ddl_seq"],
        sql="select ddl_seq.nextval",
    ),
    Case(
        id="ddl.current-database-schema",
        description="CURRENT_DATABASE and CURRENT_SCHEMA in the run's schema",
        sql="select current_database(), current_schema()",
    ),
    # --- quoted names
    Case(
        id="ddl.file-format-apostrophe",
        description="CREATE FILE FORMAT with a quoted name containing a single quote",
        refs=["#475"],
        sql="""create or replace file format "o'brien" type = csv""",
        expected=Expected(["#475"], "fakesnow pastes the name into SQL unescaped: parse error"),
    ),
    Case(
        id="ddl.schema-apostrophe",
        description="CREATE SCHEMA with a quoted name containing a single quote",
        refs=["#476"],
        sql="""create schema {database}."{schema} it's" """,
        teardown=["""drop schema if exists {database}."{schema} it's" """],
        expected=Expected(["#476"], "fakesnow pastes the name into SQL unescaped: DuckDB ParserException"),
    ),
    Case(
        id="ddl.database-apostrophe",
        description="CREATE DATABASE with a quoted name containing a single quote",
        refs=["#476"],
        sql="""create database "{schema} it's" """,
        teardown=["""drop database if exists "{schema} it's" """],
        expected=Expected(["#476"], "fakesnow pastes the name into SQL unescaped: parse error"),
    ),
    # --- temporary tables
    Case(
        id="ddl.qualified-temporary-table",
        description="A temporary table created and read with a fully qualified name",
        refs=["#472"],
        sql=[
            "create or replace temporary table {database}.{schema}.ddl_qtemp as select 1 as x",
            "select * from {database}.{schema}.ddl_qtemp",
        ],
    ),
    Case(
        id="ddl.temporary-table-after-use-schema",
        description="An unqualified name doesn't find a temporary table from another schema after USE SCHEMA",
        refs=["#479"],
        setup=[
            "create schema if not exists {database}.{schema}_S2",
            "use schema {database}.{schema}",
            "create or replace temporary table ddl_tt (x int)",
            "use schema {database}.{schema}_S2",
        ],
        sql="select * from ddl_tt",
        expected=Expected(["#479"], "DuckDB keeps all temporary tables in one catalog, searched first"),
    ),
    Case(
        id="ddl.temporary-shadows-permanent",
        description="A temporary table shadows a permanent table of the same name",
        setup=[
            "create or replace table ddl_shadow (x int)",
            "insert into ddl_shadow values (1)",
            "create or replace temporary table ddl_shadow (x int)",
            "insert into ddl_shadow values (2)",
        ],
        sql="select x from ddl_shadow",
        teardown=["drop table if exists ddl_shadow"],
    ),
    # --- INFORMATION_SCHEMA
    Case(
        id="ddl.information-schema-tables",
        description="INFORMATION_SCHEMA.TABLES row for a table: type and comment",
        setup=INFO,
        sql="select table_catalog, table_schema, table_name, table_type, comment from information_schema.tables "
        "where table_schema = '{schema}' and table_name = 'DDL_INFO'",
    ),
    Case(
        id="ddl.information-schema-columns",
        description="INFORMATION_SCHEMA.COLUMNS: types, lengths, precision, scale and nullability",
        setup=INFO,
        sql="select column_name, ordinal_position, data_type, character_maximum_length, numeric_precision, "
        "numeric_scale, is_nullable from information_schema.columns "
        "where table_schema = '{schema}' and table_name = 'DDL_INFO' order by ordinal_position",
    ),
    Case(
        id="ddl.information-schema-schemata",
        description="INFORMATION_SCHEMA.SCHEMATA lists the run's schema",
        sql="select catalog_name, schema_name from information_schema.schemata where schema_name = '{schema}'",
    ),
    Case(
        id="ddl.information-schema-views",
        description="INFORMATION_SCHEMA.VIEWS row for a view",
        setup=["create or replace view ddl_iview as select 1 as x"],
        sql="select table_name, view_definition from information_schema.views "
        "where table_schema = '{schema}' and table_name = 'DDL_IVIEW'",
        expected=Expected(
            [UNFILED], "VIEW_DEFINITION holds the DuckDB SQL fakesnow generated, not the statement as written"
        ),
    ),
    # --- SHOW and DESCRIBE
    Case(
        id="ddl.show-tables",
        description="SHOW TABLES LIKE: name, database, schema, kind, comment",
        setup=INFO,
        run=show_tables,
        expected=Expected([UNFILED], "SHOW TABLES doesn't report the table's comment"),
    ),
    Case(
        id="ddl.show-tables-columns",
        description="The columns SHOW TABLES returns",
        run=show_tables_columns,
        expected=Expected(
            [UNFILED],
            "SHOW TABLES' columns differ from current Snowflake's in order and set (kind, budget, "
            "is_interactive, row_timestamp, error_logging)",
        ),
    ),
    Case(
        id="ddl.show-columns",
        description="SHOW COLUMNS IN TABLE: name, data type JSON, nullability, kind",
        setup=INFO,
        run=show_columns,
        expected=Expected([UNFILED], "SHOW COLUMNS reports a NOT NULL column as nullable"),
    ),
    Case(id="ddl.show-schemas", description="SHOW SCHEMAS LIKE finds the run's schema", run=show_schemas),
    Case(
        id="ddl.describe-table",
        description="DESCRIBE TABLE: name, type, kind, nullability, primary key",
        setup=INFO,
        run=describe_table,
        expected=Expected([UNFILED], "SHOW SCHEMAS LIKE ignores the pattern and lists every schema"),
    ),
]
