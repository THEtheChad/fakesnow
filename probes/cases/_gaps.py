"""Differences shared by several cases, each confirmed against Snowflake (see probes/README.md)."""

from probes import UNFILED, Expected

ERROR_CODES = Expected(
    [UNFILED],
    "fakesnow maps DuckDB errors by class (Binder 2043 (02000), Catalog 2003 (42S02), Conversion 100035 (22007)), "
    "so Snowflake's specific errno and SQLSTATE are lost",
)
DUPLICATE_NAMES = Expected(
    [UNFILED],
    "in-process, the cursor builds rows with pyarrow's to_pylist() (fakesnow/cursor.py fetchmany), so columns "
    "sharing a name collapse into one; the server returns them all",
    targets=["fakesnow"],
)
FLOAT_NUMBERS = Expected(
    [UNFILED], "fakesnow returns FLOAT (REAL) where Snowflake returns NUMBER(p,s), e.g. scale 6 for division and AVG"
)
SUM_FLOAT = Expected(["#199"], "SUM comes back as FLOAT (REAL), not Snowflake's NUMBER (int or Decimal)")
DECIMAL_NOT_INT = Expected(
    ["#199"],
    "in-process, NUMBER(p,0) results come back as Decimal; the connector (and so the server) gives int",
    targets=["fakesnow"],
)
SEMI_TYPES = Expected(["#26"], "fakesnow types ARRAY and OBJECT values VARIANT (5); Snowflake ARRAY (10), OBJECT (9)")
DUCKDB_LIST = Expected(
    ["#477"],
    "fakesnow can't describe the DuckDB list the function returns: NotImplementedError (HTTP 500 in server mode)",
)
CASE_INSENSITIVE = Expected(
    ["#452"], "DuckDB resolves names case-insensitively, so a quoted lower-case name matches an unquoted one"
)
