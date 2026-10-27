"""What a target did with a case, captured as plain data that can be saved and reloaded."""

from __future__ import annotations

import base64
import datetime
import decimal
import re
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import snowflake.connector.errors
from snowflake.connector.constants import FIELD_ID_TO_NAME
from snowflake.connector.cursor import SnowflakeCursor

_CODES_PREFIX = re.compile(r"^\d{6} \([0-9A-Za-z/]{2,5}\): ")


@dataclass(frozen=True)
class ErrorInfo:
    """An exception, reduced to what's compared (errno, sqlstate, message) and its class name."""

    kind: str
    errno: int | None
    sqlstate: str | None
    message: str
    database_error: bool = True
    """Raised by the connector (or fakesnow posing as it), rather than a Python error in the probe itself."""

    @classmethod
    def from_exception(cls, e: BaseException) -> ErrorInfo:
        if isinstance(e, snowflake.connector.errors.Error):
            # the connector puts the codes in front of the message too: they're kept apart here
            message = _CODES_PREFIX.sub("", (e.msg or str(e)).strip())
            return cls(type(e).__name__, e.errno, e.sqlstate, message)
        # fakesnow raises NotImplementedError for what it doesn't support, and lets some DuckDB and sqlglot errors
        # through: both are fakesnow's answer to the statement. Anything else is a bug in the probe.
        module = type(e).__module__.lstrip("_")
        database_error = isinstance(e, NotImplementedError) or module.startswith(("duckdb", "sqlglot"))
        return cls(type(e).__name__, None, None, str(e).strip(), database_error)

    def __str__(self) -> str:
        codes = f"{self.errno} ({self.sqlstate})" if self.errno is not None else "no errno"
        return f"{self.kind} {codes}: {self.message}"


@dataclass(frozen=True)
class Column:
    name: str
    type_code: int
    precision: int | None = None
    scale: int | None = None
    internal_size: int | None = None
    """A text or binary column's length."""

    @property
    def type_name(self) -> str:
        return FIELD_ID_TO_NAME.get(self.type_code, "?")


@dataclass(frozen=True)
class ResultSet:
    columns: tuple[Column, ...]
    rows: tuple[tuple[Any, ...], ...]

    def __repr__(self) -> str:
        types = ", ".join(f"{c.type_name}({c.type_code})" for c in self.columns)
        return f"<rows {list(self.rows)!r} types [{types}]>"


@dataclass(frozen=True)
class Step:
    """One statement and what came of it: a result set or an error."""

    sql: str
    result: ResultSet | None = None
    error: ErrorInfo | None = None


@dataclass(frozen=True)
class Outcome:
    """The outcome of a case on one target.

    setup_error: the setup statement that failed, if any (then nothing else ran).
    steps: the statements under test, up to and including the first that failed.
    value / error: what a Python probe returned or raised.
    crash: the harness itself failed running the case.
    notes: things worth showing but not compared, e.g. a failed teardown.
    """

    setup_error: Step | None = None
    steps: tuple[Step, ...] = ()
    value: Any = None
    error: ErrorInfo | None = None
    crash: str | None = None
    notes: tuple[str, ...] = field(default=())


Scrub = Callable[[str], str]


def run_statement(cur: SnowflakeCursor, sql: str, template: str, scrub: Scrub) -> Step:
    """Execute a statement and capture its rows and column metadata, or its error.

    fakesnow describes a result lazily, so reading the description can fail on its own; that counts as the
    statement failing.
    """
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        description = cur.description or []
    except Exception as e:
        return Step(template, error=scrub_error(ErrorInfo.from_exception(e), scrub))
    columns = tuple(Column(scrub(d.name), d.type_code, d.precision, d.scale, d.internal_size) for d in description)
    return Step(template, result=ResultSet(columns, tuple(tuple(scrub_value(v, scrub) for v in r) for r in rows)))


def scrub_error(error: ErrorInfo, scrub: Scrub) -> ErrorInfo:
    return ErrorInfo(error.kind, error.errno, error.sqlstate, scrub(error.message), error.database_error)


def scrub_value(value: Any, scrub: Scrub) -> Any:
    """Replace run-specific names in strings, recursively, so outcomes don't depend on the run."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list | tuple):
        items = [scrub_value(v, scrub) for v in value]
        return items if isinstance(value, list) else tuple(items)
    if isinstance(value, dict):
        return {scrub_value(k, scrub): scrub_value(v, scrub) for k, v in value.items()}
    if isinstance(value, ErrorInfo):
        return scrub_error(value, scrub)
    return value


def scrubber(names: dict[str, str]) -> Scrub:
    """Replace each name (matched whole and case-insensitively) with its placeholder, e.g. {schema}."""
    if not names:
        return lambda s: s
    alternatives = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    pattern = re.compile(rf"(?<![A-Za-z0-9_$])({alternatives})(?![A-Za-z0-9$])", re.IGNORECASE)
    lookup = {n.upper(): p for n, p in names.items()}
    return lambda s: pattern.sub(lambda m: lookup[m.group(1).upper()], s)


# --- JSON encoding: plain JSON for str, int, float, bool, None and lists; everything else is tagged


def encode(value: Any) -> Any:
    match value:
        case None | bool() | int() | str():
            return value
        case float():
            return value if value == value and value not in (float("inf"), float("-inf")) else {"$float": repr(value)}
        case list():
            return [encode(v) for v in value]
        case tuple():
            return {"$tuple": [encode(v) for v in value]}
        case dict():
            return {"$dict": [[encode(k), encode(v)] for k, v in value.items()]}
        case decimal.Decimal():
            return {"$decimal": str(value)}
        case datetime.datetime():
            return {"$datetime": value.isoformat(), "tz": _tz_name(value.tzinfo)}
        case datetime.date():
            return {"$date": value.isoformat()}
        case datetime.time():
            return {"$time": value.isoformat()}
        case bytearray():
            return {"$bytearray": base64.b64encode(value).decode()}
        case bytes():
            return {"$bytes": base64.b64encode(value).decode()}
        case ErrorInfo():
            return {"$error": [value.kind, value.errno, value.sqlstate, value.message, value.database_error]}
        case Column():
            return {"$column": [value.name, value.type_code, value.precision, value.scale, value.internal_size]}
        case ResultSet():
            return {"$result": [encode(value.columns), encode(value.rows)]}
        case Step():
            return {"$step": [value.sql, encode(value.result), encode(value.error)]}
        case Outcome():
            return {
                "$outcome": {
                    "setup_error": encode(value.setup_error),
                    "steps": encode(value.steps),
                    "value": encode(value.value),
                    "error": encode(value.error),
                    "crash": value.crash,
                    "notes": list(value.notes),
                }
            }
        case _:
            return {"$repr": repr(value)}


def _tz_name(tz: datetime.tzinfo | None) -> str | None:
    # the zone's name, e.g. America/Los_Angeles, survives a round trip better than a fixed offset
    return getattr(tz, "key", None) or getattr(tz, "zone", None)


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    tag, body = next(iter(value.items()))
    match tag:
        case "$float":
            return float(body)
        case "$tuple":
            return tuple(decode(v) for v in body)
        case "$dict":
            return {decode(k): decode(v) for k, v in body}
        case "$decimal":
            return decimal.Decimal(body)
        case "$datetime":
            dt = datetime.datetime.fromisoformat(body)
            if tz := value.get("tz"):
                dt = dt.astimezone(zoneinfo.ZoneInfo(tz))
            return dt
        case "$date":
            return datetime.date.fromisoformat(body)
        case "$time":
            return datetime.time.fromisoformat(body)
        case "$bytearray":
            return bytearray(base64.b64decode(body))
        case "$bytes":
            return base64.b64decode(body)
        case "$error":
            return ErrorInfo(*body)
        case "$column":
            return Column(*body)
        case "$result":
            return ResultSet(decode(body[0]), decode(body[1]))
        case "$step":
            return Step(body[0], decode(body[1]), decode(body[2]))
        case "$outcome":
            return Outcome(
                setup_error=decode(body["setup_error"]),
                steps=decode(body["steps"]),
                value=decode(body["value"]),
                error=decode(body["error"]),
                crash=body["crash"],
                notes=tuple(body["notes"]),
            )
        case "$repr":
            return _Repr(body)
        case _:
            raise ValueError(f"unknown tag in recorded outcome: {tag}")


@dataclass(frozen=True)
class _Repr:
    """A value that couldn't be saved as such, kept as its repr."""

    text: str

    def __repr__(self) -> str:
        return self.text
