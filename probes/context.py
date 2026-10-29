"""What a Python probe gets alongside its connection."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any, TypeVar

import snowflake.connector.pandas_tools
from snowflake.connector import SnowflakeConnection
from snowflake.connector.cursor import SnowflakeCursor

from probes.outcome import ErrorInfo, ResultSet, Scrub, run_statement, scrub_value
from probes.targets import Session

T = TypeVar("T")


class ProbeContext:
    """Helpers for a Python probe, the same on every target.

    A probe returns a value made of plain data (str, numbers, datetimes, lists, tuples, dicts, and the
    ResultSet and ErrorInfo these helpers return). Run-specific names in it are replaced by placeholders before
    it's compared, as for SQL cases.
    """

    def __init__(self, session: Session, scrub: Scrub) -> None:
        self._session = session
        self._scrub = scrub
        self._opened: list[SnowflakeConnection] = []

    @property
    def target(self) -> str:
        """snowflake, fakesnow or fakesnow-server."""
        return self._session.target

    @property
    def is_snowflake(self) -> bool:
        return self._session.is_snowflake

    @property
    def database(self) -> str:
        return self._session.names.database

    @property
    def schema(self) -> str:
        """The case's own schema, the current schema when the probe starts, dropped after the case."""
        return self._session.names.schema

    def sql(self, text: str) -> str:
        """Fill in {database} and {schema}."""
        return self._session.names.render(text)

    def connect(self) -> SnowflakeConnection:
        """Open another session, in the run's database and the case's schema. It's closed after the case."""
        conn = self._session.connect()
        self._opened.append(conn)
        return conn

    def query(self, cur: SnowflakeCursor, sql: str) -> ResultSet | ErrorInfo:
        """Run a statement (placeholders filled in) and return its result set, or its error, without raising."""
        step = run_statement(cur, self.sql(sql), sql, self._scrub)
        result = step.result or step.error
        assert result
        return result

    def execute(self, cur: SnowflakeCursor, sql: str) -> ErrorInfo | None:
        """Run a statement (placeholders filled in) for its effect: None if it succeeded, else its error."""
        result = self.query(cur, sql)
        return result if isinstance(result, ErrorInfo) else None

    def attempt(self, fn: Callable[[], T]) -> T | ErrorInfo:
        """Call fn and return its result, or the error it raised, for when an error is part of the outcome."""
        try:
            return fn()
        except Exception as e:
            return ErrorInfo.from_exception(e)

    def write_pandas(self, conn: SnowflakeConnection, *args: Any, **kwargs: Any) -> Any:
        """The connector's write_pandas: fakesnow's when running in-process, the real one otherwise."""
        # looked up when called, so fakesnow.patch's replacement is used while it's active
        return snowflake.connector.pandas_tools.write_pandas(conn, *args, **kwargs)

    def scrub(self, value: Any) -> Any:
        return scrub_value(value, self._scrub)

    def close(self) -> None:
        for conn in self._opened:
            with contextlib.suppress(Exception):
                conn.close()
        self._opened.clear()
