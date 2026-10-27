"""Scalar types: NUMBER precision and scale and how values come back to Python, VARCHAR(n), BOOLEAN, BINARY."""

from probes import UNFILED, Case, Compare, Expected
from probes.cases._gaps import DECIMAL_NOT_INT, ERROR_CODES, FLOAT_NUMBERS, SUM_FLOAT

SCALE = Compare(scale=True)
NUMBERS = [
    "create or replace table types_num (i int, n number(10,2), b bigint, f float)",
    "insert into types_num values (1, 1.5, 2, 2.5), (2, 2.25, 3, 3.5)",
]
_DML_PREFIX = Expected(
    ["#473"],
    "fakesnow's message lacks Snowflake's 'DML operation to table T failed on column C with error: ' prefix",
)

CASES = [
    # --- VARCHAR(n)
    Case(
        id="types.varchar-insert-over-long",
        description="Inserting a string longer than VARCHAR(n) fails",
        refs=["#473"],
        compare=Compare(error_message=True),
        setup=["create or replace table types_vc (v varchar(5))"],
        sql="insert into types_vc values ('abcdef')",
        expected=_DML_PREFIX,
    ),
    Case(
        id="types.varchar-update-over-long",
        description="Updating to a string longer than VARCHAR(n) fails",
        refs=["#473"],
        compare=Compare(error_message=True),
        setup=["create or replace table types_vc2 (v varchar(5))", "insert into types_vc2 values ('abc')"],
        sql="update types_vc2 set v = 'abcdefg'",
        expected=_DML_PREFIX,
    ),
    Case(
        id="types.varchar-sized-cast",
        description="Casting a longer string to VARCHAR(n) fails",
        refs=["#473"],
        compare=Compare(error_message=True),
        sql="select 'abcdef'::varchar(5)",
    ),
    Case(
        id="types.varchar-try-cast-sized",
        description="TRY_CAST of a longer string to VARCHAR(n) is NULL",
        refs=["#473"],
        sql="select try_cast('abcdef' as varchar(5))",
    ),
    Case(
        id="types.varchar-number-too-long",
        description="Inserting a number whose text is longer than VARCHAR(n) fails",
        refs=["#473"],
        setup=["create or replace table types_vc3 (v varchar(2))"],
        sql="insert into types_vc3 values (123)",
    ),
    Case(
        id="types.varchar-length-metadata",
        description="A VARCHAR(n) column's length in the result metadata",
        compare=Compare(precision=True),
        setup=[
            "create or replace table types_vc4 (v varchar(5), w varchar)",
            "insert into types_vc4 values ('ab', 'cd')",
        ],
        sql="select v, w from types_vc4",
    ),
    # --- NUMBER
    Case(
        id="types.number-big-cast",
        description="Casting a 20-digit literal to NUMBER(38,0)",
        refs=["#478"],
        sql="select 99999999999999999999::number(38,0) as n",
        expected=Expected(["#478"], "fakesnow can't hold NUMBER(38,0) values outside the int64 range"),
    ),
    Case(
        id="types.number-big-insert",
        description="Inserting and reading back a 20-digit NUMBER(38,0)",
        refs=["#478"],
        setup=["create or replace table types_big (n number(38,0))"],
        sql=["insert into types_big values (99999999999999999999)", "select n from types_big"],
        expected=Expected(["#478"], "fakesnow can't hold NUMBER(38,0) values outside the int64 range"),
    ),
    Case(
        id="types.number-integer-literals",
        description="Integer literals and casts come back as int, NUMBER(38,0)",
        compare=SCALE,
        sql="select 1 as a, 1::int as b, 1::number(38,0) as c, -7::bigint as d",
    ),
    Case(
        id="types.number-precision-metadata",
        description="The precision Snowflake reports for an integer literal, COUNT and a NUMBER(10,2) column",
        compare=Compare(precision=True),
        setup=NUMBERS,
        sql="select 1 as lit, count(*) as c, max(n) as n from types_num",
        expected=Expected(
            [UNFILED], "fakesnow reports precision 38 for integer literals and COUNT; Snowflake sizes them (1, 18)"
        ),
    ),
    Case(
        id="types.number-decimal-literals",
        description="Decimal literals come back as Decimal, NUMBER(p,s) sized to the literal",
        compare=SCALE,
        sql="select 1.5, 1.50, 0.1, -12.345",
        expected=Expected(
            [UNFILED],
            "a literal's trailing zeros count in its scale (1.50 is NUMBER(3,2)); Snowflake drops them (NUMBER(2,1))",
        ),
    ),
    Case(
        id="types.number-scaled-cast",
        description="Casting to NUMBER(10,2) rounds and keeps the scale",
        compare=SCALE,
        sql="select 1::number(10,2), '12.345'::number(10,2), 12.344::number(10,2)",
    ),
    Case(
        id="types.number-division",
        description="Dividing integers gives a NUMBER with Snowflake's division scale",
        compare=SCALE,
        sql="select 7/2, 1/3, 10/5",
        expected=FLOAT_NUMBERS,
    ),
    Case(
        id="types.number-table-columns",
        description="INT, NUMBER(10,2), BIGINT and FLOAT columns read back",
        refs=["#199"],
        compare=SCALE,
        setup=NUMBERS,
        sql="select i, n, b, f from types_num order by i",
    ),
    Case(
        id="types.number-sum-int-subquery",
        description="SUM of integers comes back as int",
        refs=["#199"],
        compare=SCALE,
        sql="select sum(x) from (select 1 as x)",
        expected=SUM_FLOAT,
    ),
    Case(
        id="types.number-sum-int-column",
        description="SUM of an INT column comes back as int",
        refs=["#199"],
        compare=SCALE,
        setup=NUMBERS,
        sql="select sum(i), sum(b) from types_num",
        expected=SUM_FLOAT,
    ),
    Case(
        id="types.number-sum-decimal-column",
        description="SUM of a NUMBER(10,2) column keeps its scale",
        refs=["#199"],
        compare=SCALE,
        setup=NUMBERS,
        sql="select sum(n) from types_num",
        expected=SUM_FLOAT,
    ),
    Case(
        id="types.number-avg",
        description="AVG of an INT column",
        compare=SCALE,
        setup=NUMBERS,
        sql="select avg(i) from types_num",
        expected=FLOAT_NUMBERS,
    ),
    Case(
        id="types.number-count-min-max",
        description="COUNT, MIN and MAX of integer and decimal columns",
        compare=SCALE,
        setup=NUMBERS,
        sql="select count(*), count(i), min(i), max(n) from types_num",
    ),
    Case(
        id="types.number-arithmetic-on-column",
        description="Arithmetic on an INT column stays integral",
        compare=SCALE,
        setup=NUMBERS,
        sql="select i + 1, i * 2, i - b from types_num order by i",
    ),
    Case(
        id="types.number-round-trunc",
        description="ROUND and TRUNC with and without a scale",
        compare=SCALE,
        sql="select round(2.567, 2), round(2.5), round(-2.5), trunc(2.567, 1)",
        expected=DECIMAL_NOT_INT,
    ),
    Case(
        id="types.number-cast-rounds-half-away",
        description="Casting a decimal to an integer rounds half away from zero",
        sql="select 2.5::int, (-2.5)::int, 3.5::int",
    ),
    Case(
        id="types.number-not-recognized",
        description="Casting a non-numeric string to NUMBER fails",
        sql="select 'abc'::number",
        expected=ERROR_CODES,
    ),
    Case(
        id="types.number-try-to-number",
        description="TRY_TO_NUMBER on good and bad input",
        compare=SCALE,
        sql="select try_to_number('abc'), try_to_number('12'), try_to_number('12.5', 10, 1)",
        expected=DECIMAL_NOT_INT,
    ),
    Case(
        id="types.float-values",
        description="FLOAT arithmetic and literals",
        sql="select 1.5::float * 2, 1e10::float, 0.1::float + 0.2::float",
    ),
    # --- other scalars
    Case(
        id="types.boolean-values",
        description="BOOLEAN literals and casts from strings and numbers",
        sql="select true, false, 'yes'::boolean, 'off'::boolean, 1::boolean, 0::boolean",
        expected=Expected([UNFILED], "fakesnow can't cast 'on' or 'off' to BOOLEAN; Snowflake accepts them"),
    ),
    Case(
        id="types.binary-values",
        description="BINARY values from TO_BINARY",
        sql="select to_binary('ab', 'hex'), to_binary('hi', 'utf-8')",
        expected=Expected(
            [UNFILED], "in-process, BINARY comes back as bytes; the connector gives bytearray", targets=["fakesnow"]
        ),
    ),
    Case(
        id="types.string-unicode-length",
        description="LENGTH counts characters, not bytes",
        sql="select length('héllo'), length('日本')",
    ),
    Case(
        id="types.typeof-system",
        description="SYSTEM$TYPEOF of literals",
        sql="select system$typeof(1), system$typeof(1.5), system$typeof('a'), system$typeof(1.5::float)",
        expected=Expected([UNFILED], "SYSTEM$TYPEOF isn't implemented"),
    ),
]
