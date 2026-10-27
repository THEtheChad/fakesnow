"""Error codes: errno and SQLSTATE for missing objects, bad SQL and bad data."""

from probes import Case, Expected
from probes.cases._gaps import ERROR_CODES

T = ["create or replace table errs_t (x int, y varchar)"]

CASES = [
    Case(
        id="errors.missing-table",
        description="Selecting from a table that doesn't exist",
        sql="select * from errs_no_such_table",
    ),
    Case(
        id="errors.missing-column",
        description="Selecting a column that doesn't exist",
        setup=T,
        sql="select no_such_col from errs_t",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.missing-column-in-where",
        description="A WHERE clause naming a column that doesn't exist",
        setup=T,
        sql="select x from errs_t where no_such_col = 1",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.missing-schema",
        description="Selecting from a table in a schema that doesn't exist",
        sql="select * from errs_no_such_schema.t",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.missing-database",
        description="Selecting from a table in a database that doesn't exist",
        sql="select * from errs_no_such_db.s.t",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.use-missing-schema",
        description="USE SCHEMA of a schema that doesn't exist",
        sql="use schema errs_no_such_schema",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.use-missing-database",
        description="USE DATABASE of a database that doesn't exist",
        sql="use database errs_no_such_db",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.drop-missing-table",
        description="DROP TABLE of a table that doesn't exist",
        sql="drop table errs_no_such_table",
    ),
    Case(
        id="errors.insert-missing-table",
        description="INSERT into a table that doesn't exist",
        sql="insert into errs_no_such_table values (1)",
    ),
    Case(
        id="errors.create-existing-table",
        description="CREATE TABLE of a table that exists",
        setup=T,
        sql="create table errs_t (x int)",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.syntax",
        description="A syntax error",
        sql="select from where",
    ),
    Case(
        id="errors.unknown-function",
        description="Calling a function that doesn't exist",
        sql="select errs_no_such_function(1)",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.division-by-zero",
        description="Integer division by zero",
        sql="select 1/0",
        expected=Expected(["#483"], "1/0 returns inf (FLOAT) instead of failing with 100051 Division by zero"),
    ),
    Case(
        id="errors.not-null-violation",
        description="Inserting NULL into a NOT NULL column",
        setup=["create or replace table errs_nn (x int not null)"],
        sql="insert into errs_nn values (null)",
        expected=Expected(
            ["#483"],
            "a NOT NULL violation raises DuckDB's ConstraintException, not "
            "IntegrityError 100072 (HTTP 500 in server mode)",
        ),
    ),
    Case(
        id="errors.insert-too-many-values",
        description="INSERT with more values than columns",
        setup=T,
        sql="insert into errs_t values (1, 'a', 2)",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.insert-bad-number",
        description="INSERT of a non-numeric string into a NUMBER column",
        setup=T,
        sql="insert into errs_t values ('abc', 'a')",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.ambiguous-column",
        description="An unqualified column name present in both joined tables",
        setup=[*T, "create or replace table errs_t2 (x int)"],
        sql="select x from errs_t, errs_t2",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.not-in-group-by",
        description="Selecting a column that's neither grouped nor aggregated",
        setup=T,
        sql="select x, count(*) from errs_t",
        expected=ERROR_CODES,
    ),
    Case(
        id="errors.wrong-argument-count",
        description="A built-in function with the wrong number of arguments",
        sql="select split_part('a')",
        expected=ERROR_CODES,
    ),
]
