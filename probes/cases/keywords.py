"""Words as unquoted table and column names: DuckDB reserves some that Snowflake doesn't, and vice versa (#481).

A generated set: two cases per word, as a table name and as a column name.
"""

from probes import Case, Expected

WORDS = [
    "SEMI", "ANTI", "ASOF", "POSITIONAL", "PIVOT", "UNPIVOT", "LAMBDA", "MACRO", "SUMMARIZE", "COLUMNS",
    "STRUCT", "MAP", "LIST", "UNION", "ARRAY", "EXCLUDE", "REPLACE", "QUALIFY", "WINDOW", "OFFSET",
    "LIMIT", "RETURNING", "ANALYSE", "ANALYZE", "SIMILAR", "GLOB", "ILIKE", "VARIADIC", "DO", "PLACING",
    "SYMMETRIC", "ASYMMETRIC", "LATERAL", "COLLATE", "DEFERRABLE", "INITIALLY", "USING", "BOTH", "LEADING",
    "TRAILING", "OVERLAPS", "FREEZE", "CONCURRENTLY", "VERBOSE", "TABLESAMPLE", "SAMPLE", "ATTACH", "DETACH",
    "PRAGMA", "INSTALL", "LOAD", "EXPORT", "IMPORT", "TRY_CAST", "TYPE", "ENUM", "DEFAULT", "FILTER", "OVER",
    "RANGE", "ROWS", "GROUPS", "NULLS", "FIRST", "LAST", "KEY", "VALUE", "DATA", "SOURCE", "TARGET", "ROW",
    "MATCH",
]  # fmt: skip

# from #481, checked against Snowflake 10.38 and fakesnow 0.12.1
_DUCKDB_RESERVED = ["SEMI", "ANTI", "POSITIONAL", "PIVOT", "UNPIVOT", "LAMBDA", "SUMMARIZE", "RETURNING", "SIMILAR"]
_DUCKDB_RESERVED += ["GLOB", "OVERLAPS", "FREEZE", "CONCURRENTLY", "VERBOSE"]
# from #481, checked again here: Snowflake creates these tables but refuses the name unquoted in SELECT
_CREATE_ONLY = ["ASOF", "LATERAL", "USING"]
# not in #481, which didn't read cursor.description: fakesnow describes a result by parsing the query again as
# DuckDB SQL, and sqlglot's DuckDB dialect reserves these
_DESCRIBE = (
    "fakesnow's cursor.description re-parses the query as DuckDB SQL: raw sqlglot ParseError (HTTP 500 in server)"
)
_ACCEPTS = "fakesnow refuses it, Snowflake accepts it"
_REFUSES = "Snowflake refuses it, fakesnow accepts it"
TABLE_DIFFERS = {
    **dict.fromkeys(_DUCKDB_RESERVED, _ACCEPTS),
    **dict.fromkeys(["ROW", "ROWS"], _REFUSES),
    **dict.fromkeys(
        _CREATE_ONLY, "Snowflake creates the table, then refuses the name in SELECT; fakesnow refuses CREATE"
    ),
    "INSTALL": _DESCRIBE,
}
COLUMN_DIFFERS = {
    **dict.fromkeys([*_DUCKDB_RESERVED, *_CREATE_ONLY, "EXCLUDE"], _ACCEPTS),
    **dict.fromkeys(["TRY_CAST", "DEFAULT", "ROW", "ROWS"], _REFUSES),
    **dict.fromkeys(["UNION", "SAMPLE"], "Snowflake refuses it in CREATE TABLE, fakesnow only in the SELECT"),
    **dict.fromkeys(["MAP", "INSTALL"], _DESCRIBE),
}


def _expected(differs: dict[str, str], word: str) -> Expected | None:
    return Expected(["#481"], differs[word]) if word in differs else None


def cases(word: str) -> list[Case]:
    w = word.lower()
    return [
        Case(
            id=f"keywords.{w}.table",
            description=f"{word} as an unquoted table name",
            refs=["#481"],
            sql=[f"create or replace table {word} (x int)", f"select x from {word}"],
            expected=_expected(TABLE_DIFFERS, word),
        ),
        Case(
            id=f"keywords.{w}.column",
            description=f"{word} as an unquoted column name",
            refs=["#481"],
            sql=[f"create or replace table kw_{w} ({word} int)", f"select {word} from kw_{w}"],
            expected=_expected(COLUMN_DIFFERS, word),
        ),
    ]


assert len(WORDS) == len(set(WORDS)) == 72
CASES = [c for word in WORDS for c in cases(word)]
