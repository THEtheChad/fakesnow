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

# checked against Snowflake 10.38 and the fork's fakesnow, which quotes the names DuckDB reserves (#482): what's
# left is mostly sqlglot's Snowflake parser (tobymao/sqlglot#8805) and DuckDB generator (tobymao/sqlglot#8804)
_PARSER = ["#481", "tobymao/sqlglot#8805"]
_GENERATOR = "tobymao/sqlglot#8804"
_ACCEPTS = "fakesnow refuses it, Snowflake accepts it"
_REFUSES = ("Snowflake refuses it, fakesnow accepts it", ["#481"])
_CREATE_ONLY = "Snowflake creates the table, then refuses the name in SELECT"
_SELECT_ONLY = ("Snowflake refuses it in CREATE TABLE, fakesnow only in the SELECT", ["#481"])
# fakesnow describes a result by parsing its generated DuckDB SQL again, and INSTALL isn't quoted in it
_DESCRIBE = (
    "cursor.description re-parses the generated DuckDB SQL: raw sqlglot ParseError",
    ["#481", "tobymao/sqlglot#8804"],
)
TABLE_DIFFERS: dict[str, tuple[str, list[str]]] = {
    **dict.fromkeys(["RETURNING", "GLOB", "CONCURRENTLY"], (_ACCEPTS, _PARSER)),
    **dict.fromkeys(["LATERAL", "USING"], (f"{_CREATE_ONLY}; fakesnow refuses the CREATE", _PARSER)),
    "ASOF": (f"{_CREATE_ONLY}; fakesnow accepts both", ["#481"]),
    **dict.fromkeys(["ROW", "ROWS"], _REFUSES),
    "INSTALL": _DESCRIBE,
}
COLUMN_DIFFERS: dict[str, tuple[str, list[str]]] = {
    **dict.fromkeys(["EXCLUDE", "RETURNING", "GLOB", "LATERAL", "USING"], (_ACCEPTS, _PARSER)),
    **dict.fromkeys(["TRY_CAST", "DEFAULT", "ROW", "ROWS"], _REFUSES),
    **dict.fromkeys(["UNION", "SAMPLE"], _SELECT_ONLY),
    "INSTALL": _DESCRIBE,
}


def _expected(differs: dict[str, tuple[str, list[str]]], word: str) -> Expected | None:
    if word not in differs:
        return None
    reason, refs = differs[word]
    return Expected(refs, reason)


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
