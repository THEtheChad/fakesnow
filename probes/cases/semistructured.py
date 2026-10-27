"""VARIANT, ARRAY and OBJECT: construction, access paths, casts, and their result metadata."""

from snowflake.connector import SnowflakeConnection

from probes import UNFILED, Case, Compare, Expected, ProbeContext
from probes.cases._gaps import DUCKDB_LIST, SEMI_TYPES

SEMIS = ["create or replace table semis_t (a array, o object, v variant)"]
DOC = [
    "create or replace table semis_v (v variant)",
    """insert into semis_v select parse_json('{"a": {"b": [10, 20]}, "s": "x", "n": 1.5, "z": null}')""",
]
_ARRAY_TYPE = "fakesnow types it VARIANT (5), Snowflake ARRAY (10)"


def describe_semis(conn: SnowflakeConnection, ctx: ProbeContext) -> list[tuple[str, str]]:
    cur = conn.cursor()
    cur.execute("create or replace table semis_t (a array, o object, v variant)")
    cur.execute("describe table semis_t")
    return [(r[0], r[1]) for r in cur.fetchall()]


CASES = [
    # --- construction
    Case(
        id="semis.array-to-string-compact",
        description="ARRAY_TO_STRING of ARRAY_CONSTRUCT_COMPACT drops the NULLs",
        refs=["#466"],
        sql="select array_to_string(array_construct_compact('a', null, 'b'), ', ')",
    ),
    Case(
        id="semis.array-slice-of-compact",
        description="ARRAY_SLICE of ARRAY_CONSTRUCT_COMPACT",
        refs=["#466"],
        sql="select array_slice(array_construct_compact(1, null, 2, 3), 0, 2)",
        expected=DUCKDB_LIST,
    ),
    Case(
        id="semis.array-slice-direct",
        description="ARRAY_SLICE selected directly",
        refs=["#477"],
        sql="select array_slice(array_construct(1, 2, 3), 0, 2)",
        expected=Expected(["#477"], "fakesnow can't describe the DuckDB list ARRAY_SLICE returns: NotImplementedError"),
    ),
    Case(
        id="semis.array-construct-aliased",
        description="ARRAY_CONSTRUCT with an alias",
        refs=["#477"],
        sql="select array_construct(1, 2) as a",
        expected=Expected(["#477"], "fakesnow can't describe an aliased ARRAY_CONSTRUCT: NotImplementedError"),
    ),
    Case(
        id="semis.array-construct-unaliased",
        description="ARRAY_CONSTRUCT without an alias is an ARRAY",
        refs=["#477"],
        sql="select array_construct(1, 2)",
        expected=Expected(["#477"], _ARRAY_TYPE),
    ),
    Case(
        id="semis.formatting",
        description="The raw text of a VARIANT, OBJECT and ARRAY, whitespace included (no normalisation)",
        refs=["#477"],
        compare=Compare(types=False, normalise=False),
        sql="""select parse_json('{"a": 1, "b": [1, 2]}'), object_construct('k', 1), array_construct(1, 2)""",
        expected=Expected(["#477"], "Snowflake indents JSON text by two spaces, fakesnow returns it compact"),
    ),
    Case(
        id="semis.object-construct",
        description="OBJECT_CONSTRUCT drops NULL values; OBJECT_CONSTRUCT_KEEP_NULL keeps them (as VARCHAR)",
        sql="select object_construct('a', 1, 'b', 'x', 'c', null)::varchar as o, "
        "object_construct_keep_null('a', 1, 'c', null)::varchar as k",
    ),
    Case(
        id="semis.object-construct-key-order",
        description="The key order of OBJECT_CONSTRUCT's result (as VARCHAR)",
        sql="select object_construct('b', 1, 'a', 2)::varchar as o",
        expected=Expected([UNFILED], "OBJECT_CONSTRUCT keeps the keys in argument order; Snowflake sorts them"),
    ),
    Case(
        id="semis.parse-json-scalars",
        description="PARSE_JSON of a number, string, null and mixed array",
        sql="""select parse_json('1') as n, parse_json('"x"') as s, parse_json('null') as z, """
        """parse_json('[1, "a", null]') as a""",
    ),
    Case(
        id="semis.parse-json-invalid",
        description="PARSE_JSON of invalid JSON fails",
        sql="select parse_json('{bad')",
        expected=Expected(
            [UNFILED],
            "invalid JSON raises DuckDB's InvalidInputException, not ProgrammingError 100069 (HTTP 500 in server mode)",
        ),
    ),
    Case(
        id="semis.try-parse-json",
        description="TRY_PARSE_JSON of invalid JSON is NULL",
        sql="select try_parse_json('{bad') as t",
    ),
    Case(
        id="semis.array-agg",
        description="ARRAY_AGG ... WITHIN GROUP (ORDER BY ...)",
        sql="select (array_agg(x) within group (order by x))::varchar as a "
        "from (select column1 as x from values (2), (1), (3))",
    ),
    Case(
        id="semis.object-agg",
        description="OBJECT_AGG of key/value pairs",
        sql="select object_agg(k, v::variant)::varchar as o "
        "from (select column1 as k, column2 as v from values ('a', 1), ('b', 2))",
    ),
    Case(
        id="semis.array-functions",
        description="ARRAY_SIZE, ARRAY_CONTAINS and ARRAY_POSITION",
        sql="select array_size(parse_json('[1, 2, 3]')), array_contains(2::variant, array_construct(1, 2)), "
        "array_position(2::variant, array_construct(1, 2))",
    ),
    Case(
        id="semis.typeof",
        description="TYPEOF of VARIANT values",
        sql="""select typeof(parse_json('1')), typeof(parse_json('1.5')), typeof(parse_json('"a"')), """
        """typeof(parse_json('[1]')), typeof(parse_json('{"a": 1}')), typeof(parse_json('null'))""",
        expected=Expected([UNFILED], "TYPEOF returns DuckDB's 'JSON' for every VARIANT"),
    ),
    Case(
        id="semis.object-keys",
        description="OBJECT_KEYS of an object",
        sql="""select object_keys(parse_json('{"b": 1, "a": 2}')) as k""",
        expected=DUCKDB_LIST,
    ),
    Case(
        id="semis.constructed-type-codes",
        description="Result type codes of OBJECT_CONSTRUCT, ARRAY_AGG, OBJECT_AGG and SPLIT",
        refs=["#26"],
        compare=Compare(rows=False),
        sql="select object_construct('a', 1) as o, array_agg(1) as a, object_agg('k', 1::variant) as oa, "
        "split('a', ',') as s",
        expected=SEMI_TYPES,
    ),
    # --- access paths
    Case(
        id="semis.path-colon-dot-bracket",
        description="col:path with dots and [n] returns VARIANT",
        setup=DOC,
        sql="select v:a.b[1] as b1, v:s as s, v:n as n from semis_v",
    ),
    Case(
        id="semis.path-brackets",
        description="col['key'] and col:\"key\" access",
        setup=DOC,
        sql="""select v['a']['b'][0] as b0, v:"s" as s from semis_v""",
    ),
    Case(
        id="semis.path-casts",
        description=":: casts of path values to STRING, INT and FLOAT, and of a missing key",
        setup=DOC,
        sql="select v:s::string as s, v:a.b[0]::int as i, v:n::float as f, v:missing::string as m from semis_v",
    ),
    Case(
        id="semis.path-cast-number-scale",
        description="A path value cast to NUMBER(10,2)",
        compare=Compare(scale=True),
        setup=DOC,
        sql="select v:n::number(10,2) as n from semis_v",
    ),
    Case(
        id="semis.get-path-and-get",
        description="GET_PATH and GET",
        setup=DOC,
        sql="select get_path(v, 'a.b[1]') as p, get(v, 's') as g, get(v:a.b, 0) as i from semis_v",
    ),
    Case(
        id="semis.json-null-vs-sql-null",
        description="A JSON null is a VARIANT null, not SQL NULL; a missing key is SQL NULL",
        setup=DOC,
        sql="select v:z as z, v:z is null as z_is_null, is_null_value(v:z) as z_null_value, v:missing is null as m "
        "from semis_v",
    ),
    Case(
        id="semis.string-to-variant",
        description="A string cast to VARIANT is a JSON string",
        sql="select 'x'::variant as v, to_variant(1) as n",
        expected=Expected([UNFILED], "a string cast to VARIANT is parsed as JSON instead of becoming a JSON string"),
    ),
    Case(
        id="semis.variant-comparison",
        description="Comparing a VARIANT path with a literal",
        setup=DOC,
        sql="select v:s = 'x' as eq, v:a.b[0] > 5 as gt from semis_v",
        expected=Expected([UNFILED], "comparing a VARIANT with a string literal parses the literal as JSON and fails"),
    ),
    # --- metadata
    Case(
        id="semis.describe-array-object",
        description="DESCRIBE TABLE types ARRAY, OBJECT and VARIANT columns",
        refs=["#469"],
        run=describe_semis,
    ),
    Case(
        id="semis.information-schema-array-object",
        description="INFORMATION_SCHEMA.COLUMNS data types of ARRAY, OBJECT and VARIANT columns",
        refs=["#469"],
        setup=SEMIS,
        sql="select column_name, data_type from information_schema.columns "
        "where table_schema = '{schema}' and table_name = 'SEMIS_T' order by ordinal_position",
    ),
    Case(
        id="semis.result-type-codes",
        description="Result type codes of ARRAY, OBJECT and VARIANT columns",
        refs=["#469", "#26"],
        setup=SEMIS,
        sql="select * from semis_t",
        expected=Expected(
            ["#26"], "fakesnow types ARRAY and OBJECT columns VARIANT (5), Snowflake ARRAY (10) and OBJECT (9)"
        ),
    ),
]
