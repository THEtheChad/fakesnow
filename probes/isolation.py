"""The leak check: shared state a case leaves changed, which dropping its own schema doesn't undo.

Each case runs in a schema of its own, created before its setup and dropped after its teardown, so what it creates
there can't reach later cases. What still can is checked here, after the drop, against a snapshot from before the
case (PolDet's approach: Gyori et al., ISSTA 2015). Anything new, gone or changed is a leak: the runner undoes it, so
later cases aren't affected, and reports it against the case that left it.

What's checked, per target:

- schemas in the run's database (except other runs'), and databases named after the run: every target;
- the main session's parameters, from SHOW PARAMETERS IN SESSION: Snowflake only. fakesnow lists fixed values and
  can't change any but AUTOCOMMIT, which the runner resets after every case. Each SHOW is a round trip, so it runs
  only after cases that could change a parameter: Python probes, and SQL with ALTER SESSION, CALL, EXECUTE IMMEDIATE
  or a block (BEGIN/DECLARE);
- the main session's role and warehouse, as the connector reports them: Snowflake only (fakesnow has neither).

The current database and schema aren't: the next case's CREATE SCHEMA makes its own schema current.

fakesnow keeps temporary tables in one DuckDB catalog, so they outlive the schema they were created in (Snowflake
drops them with it, see case ddl.drop-schema-drops-temporary-table). On fakesnow targets the runner drops the
temporary tables a case created, as dropping its schema does on Snowflake, without reporting them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import snowflake.connector.errors

from probes.model import Case
from probes.outcome import Scrub
from probes.targets import Session

# statements that can change a session parameter: anything else needs no SHOW PARAMETERS after it
_PARAMETER_CHANGES = re.compile(r"\b(alter\s+session|call|execute\s+immediate|begin|declare)\b", re.IGNORECASE)


@dataclass(frozen=True)
class State:
    """Shared state a case could leave changed: what a snapshot found."""

    schemas: frozenset[str] = frozenset()
    """In the run's database."""
    databases: frozenset[str] = frozenset()
    """Named after the run."""
    context: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    """The main session's role and warehouse."""
    parameters: Mapping[str, tuple[str, str]] | None = None
    """Session parameter -> (value, type); None when not listed."""
    temporary_tables: frozenset[str] = frozenset()
    """fakesnow's, see the module docstring."""


@dataclass(frozen=True)
class Leak:
    description: str
    undo: str | None
    """SQL that undoes it, if the runner can."""


def checks_parameters(session: Session, case: Case) -> bool:
    """Whether to list session parameters after the case: only Snowflake can change them."""
    statements = (*case.setup, *case.statements, *case.teardown)
    return session.is_snowflake and (case.run is not None or any(_PARAMETER_CHANGES.search(s) for s in statements))


class LeakCheck:
    """Snapshots a target's shared state between cases, and reports and undoes what a case changed.

    cleanup=False leaves leaks in place (still reporting each once), e.g. to find what a leak affects.
    """

    def __init__(self, session: Session, *, cleanup: bool = True) -> None:
        self.session = session
        self.cleanup = cleanup
        self.before = self.snapshot(parameters=session.is_snowflake)

    def after(self, case: Case, scrub: Scrub) -> tuple[str, ...]:
        """What the case leaked, with run-specific names scrubbed, once it's been undone."""
        after = self.snapshot(parameters=checks_parameters(self.session, case))
        if after.parameters is None:
            after = replace(after, parameters=self.before.parameters)
        for table in sorted(after.temporary_tables - self.before.temporary_tables):
            self._execute(f'drop table if exists temp.main."{_quoted(table)}"')
        after = replace(after, temporary_tables=self.before.temporary_tables)
        leaks = list(self.changes(self.before, after))
        descriptions = [leak.description for leak in leaks]
        if leaks and self.cleanup:
            descriptions = [self._undo(leak) for leak in leaks]
            changed_parameters = after.parameters != self.before.parameters
            after = self.snapshot(parameters=changed_parameters)
            if not changed_parameters:
                after = replace(after, parameters=self.before.parameters)
        self.before = after
        return tuple(scrub(d) for d in descriptions)

    def snapshot(self, *, parameters: bool) -> State:
        names = self.session.run_names
        sql = (
            f"select 'schema', schema_name from {names.database}.information_schema.schemata union all "
            f"select 'database', database_name from {names.database}.information_schema.databases "
            f"where startswith(database_name, '{names.run}')"
        )
        if not self.session.is_snowflake:
            # DuckDB's own catalog function, which fakesnow passes through; _fs_ tables are fakesnow's
            sql += " union all select 'temporary table', table_name from duckdb_tables() where temporary"
        found: dict[str, set[str]] = {"schema": set(), "database": set(), "temporary table": set()}
        for kind, name in self._query(sql):
            if names.is_other_run(name) or (kind == "database" and not str(name).startswith(names.run)):
                continue
            if not (kind == "temporary table" and str(name).startswith("_fs_")):
                found[kind].add(str(name))
        conn: Any = self.session.conn
        state = State(
            frozenset(found["schema"]),
            frozenset(found["database"]),
            {"role": getattr(conn, "role", None), "warehouse": getattr(conn, "warehouse", None)},
            temporary_tables=frozenset(found["temporary table"]),
        )
        if parameters:
            cur = self.session.conn.cursor()
            cur.execute("show parameters in session")
            columns = [d.name.lower() for d in cur.description]
            listed = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
            state = replace(state, parameters={str(p["key"]): (str(p["value"]), str(p["type"])) for p in listed})
        return state

    def changes(self, before: State, after: State) -> Iterator[Leak]:
        """The leaks between two snapshots, each with how to undo it."""
        names = self.session.run_names
        for name in sorted(after.schemas - before.schemas):
            where = f"{names.database}.{name}"
            # On Snowflake, only what's named after the run is certainly the run's to drop: the database is shared.
            mine = not self.session.is_snowflake or name.startswith(names.run)
            undo = f'drop schema if exists {names.database}."{_quoted(name)}" cascade' if mine else None
            yield Leak(f"schema {where} left behind" + ("" if mine else ", not named after the run"), undo)
        for name in sorted(before.schemas - after.schemas):
            yield Leak(f"schema {names.database}.{name} dropped", None)
        for name in sorted(after.databases - before.databases):
            yield Leak(f"database {name} left behind", f'drop database if exists "{_quoted(name)}" cascade')
        for name in sorted(before.databases - after.databases):
            yield Leak(f"database {name} dropped", None)
        for key, old in before.context.items():
            new = after.context.get(key)
            if new != old:
                undo = f'use {key} "{_quoted(old)}"' if old else None
                yield Leak(f"session {key} changed from {old} to {new}", undo)
        old_parameters, new_parameters = before.parameters or {}, after.parameters or {}
        for key in sorted(old_parameters.keys() & new_parameters.keys()):
            (old, kind), (new, _) = old_parameters[key], new_parameters[key]
            if new != old:
                value = f"'{_escaped(old)}'" if kind.upper() == "STRING" else old
                undo = f"alter session set {key} = {value}"
                yield Leak(f"session parameter {key} changed from {old!r} to {new!r}", undo)

    def _undo(self, leak: Leak) -> str:
        if leak.undo is None:
            return f"{leak.description} (not undone)"
        try:
            self._execute(leak.undo)
        except snowflake.connector.errors.Error as e:
            # codes only: a pinned outcome shouldn't depend on the wording of a message
            return f"{leak.description} (undoing it failed: {e.errno} ({e.sqlstate}))"
        return leak.description

    def _query(self, sql: str) -> list[tuple[Any, ...]]:
        cur = self.session.conn.cursor()
        cur.execute(sql)
        return [tuple(row) for row in cur.fetchall()]

    def _execute(self, sql: str) -> None:
        self.session.conn.cursor().execute(sql)


def _quoted(name: str) -> str:
    return name.replace('"', '""')


def _escaped(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
