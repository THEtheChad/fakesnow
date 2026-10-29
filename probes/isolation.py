"""The leak check: shared state a case leaves changed, which dropping its own schema doesn't undo.

Each case runs in a schema of its own, created before its setup and dropped after its teardown, so what it creates
there can't reach later cases. What still can is checked here, after the drop, against a snapshot from before the
case (PolDet's approach: Gyori et al., ISSTA 2015). Anything new, gone or changed is a leak: the runner undoes it, so
later cases aren't affected, and reports it against the case that left it.

What's checked, per target:

- schemas in the run's database (except other runs'), databases named after the run, and the main session's
  temporary tables whose schema still exists: every target (on Snowflake, those in the run's database);
- the main session's parameters (SHOW PARAMETERS IN SESSION) and variables (SHOW VARIABLES): Snowflake only.
  fakesnow lists fixed parameters, can't change any but AUTOCOMMIT (which the runner resets), and lists no
  variables even when some are set, so its variables go unchecked;
- the main session's role and warehouse, as the connector reports them: Snowflake only (fakesnow has neither).

The current database and schema aren't: the next case's CREATE SCHEMA makes its own schema current.

Each SHOW is a round trip to Snowflake, so there the snapshot lists only what the case could have changed, judged
from its statements by reach(); fakesnow's snapshot is in-process (or local) and lists everything after every case.
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

# The first keyword of a statement that can't change the session (its parameters, variables, autocommit or
# transaction) nor what the leak check lists: queries and DML, SHOW and DESCRIBE, stage file commands, COMMENT,
# COMMIT and ROLLBACK (which end a transaction, never start one), and USE (the current database and schema, which
# the next case's CREATE SCHEMA resets, or the role and warehouse, which the check reads from the connector).
_CANT_CHANGE = frozenset(
    {
        *("select", "with", "values", "show", "desc", "describe", "explain"),
        *("insert", "update", "delete", "merge", "copy", "truncate", "list", "ls", "get", "put", "remove", "rm"),
        *("comment", "commit", "rollback", "use"),
    }
)
# DDL can't change the session either, but can change what the leak check lists when it names one of those kinds.
# Not ALTER: ALTER SESSION, USER and ACCOUNT set parameters.
_DDL = frozenset({"create", "drop", "undrop"})
_LISTED_KINDS = frozenset({"schema", "schemas", "database", "databases", "temporary", "temp", "volatile"})

# comments, then quoted text (strings, $$ bodies, quoted identifiers), then what's left unterminated, words, the rest
_TOKEN = re.compile(
    r"""(?P<comment>--[^\n]*|//[^\n]*|/\*.*?\*/)
    |(?P<quoted>'(?:[^'\\]|\\.|'')*'|\$\$.*?\$\$|"(?:[^"]|"")*")
    |(?P<unterminated>'|"|\$\$|/\*)
    |(?P<word>[A-Za-z_][A-Za-z0-9_$]*)
    |(?P<other>\S)""",
    re.DOTALL | re.VERBOSE,
)


@dataclass(frozen=True)
class Reach:
    """What a case could change beyond its own schema, judged from its statements."""

    session: bool
    """The main session: its parameters, variables, autocommit or an open transaction."""
    objects: bool
    """Schemas, databases or temporary tables."""


EVERYTHING = Reach(session=True, objects=True)


def reach(case: Case) -> Reach:
    """Conservatively: a Python probe, or any statement the scan can't place, could change anything."""
    if case.run:
        return EVERYTHING
    objects = False
    for sql in (*case.setup, *case.statements, *case.teardown):
        words = _words(sql)
        if words is None or (words[0] not in _CANT_CHANGE and words[0] not in _DDL):
            return EVERYTHING
        objects |= words[0] in _DDL and not _LISTED_KINDS.isdisjoint(words)
    return Reach(session=False, objects=objects)


def _words(sql: str) -> list[str] | None:
    """A statement's words, lower case, outside comments and quotes; None unless it's plainly one statement."""
    words: list[str] = []
    ended = False
    for token in _TOKEN.finditer(sql.replace("{database}", "D").replace("{schema}", "S")):
        if token["comment"]:
            continue
        if ended or token["unterminated"]:
            return None
        if token["word"]:
            words.append(token["word"].lower())
        elif token["other"] == ";":
            ended = True
        elif not words and token["other"] != "(":
            return None  # nothing comes before the first keyword but parentheses
    return words or None


@dataclass(frozen=True)
class State:
    """Shared state a case could leave changed: what a snapshot found."""

    schemas: frozenset[str] = frozenset()
    """In the run's database."""
    databases: frozenset[str] = frozenset()
    """Named after the run."""
    temporary_tables: frozenset[tuple[str, str, str]] = frozenset()
    """The main session's, as (database, schema, name), whose schema still exists."""
    context: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    """The main session's role and warehouse."""
    parameters: Mapping[str, tuple[str, str]] | None = None
    """Session parameter -> (value, type); None when not listed."""
    variables: Mapping[str, tuple[str, str]] | None = None
    """Session variable -> (value, type); None when not listed."""


@dataclass(frozen=True)
class Leak:
    description: str
    undo: str | None
    """SQL that undoes it, if the runner can."""


class LeakCheck:
    """Snapshots a target's shared state between cases, and reports and undoes what a case changed.

    cleanup=False leaves leaks in place (still reporting each once), e.g. to find what a leak affects. It also lists
    everything after every case: a later case could undo a leak left in place without the snapshot noticing.
    """

    def __init__(self, session: Session, *, cleanup: bool = True) -> None:
        self.session = session
        self.cleanup = cleanup
        self.before = self.snapshot(EVERYTHING, State())

    @property
    def scope(self) -> str:
        """What the check covers on this target, for reports."""
        if self.session.is_snowflake:
            return "schemas, databases, temporary tables, session parameters and variables, role and warehouse"
        return "schemas, databases, temporary tables; variables unchecked (fakesnow doesn't list them)"

    def after(self, case: Case, scrub: Scrub) -> tuple[str, ...]:
        """What the case leaked, with run-specific names scrubbed, once it's been undone."""
        listed = reach(case) if self.cleanup else EVERYTHING
        after = self.snapshot(listed, self.before)
        leaks = list(self.changes(self.before, after))
        descriptions = [leak.description for leak in leaks]
        if leaks and self.cleanup:
            descriptions = [self._undo(leak) for leak in leaks]
            after = self.snapshot(listed, self.before)
        self.before = after
        return tuple(scrub(d) for d in descriptions)

    def snapshot(self, listed: Reach, unlisted: State) -> State:
        """List what's shared that a case could have changed; the rest is as in unlisted."""
        conn: Any = self.session.conn
        context = {"role": getattr(conn, "role", None), "warehouse": getattr(conn, "warehouse", None)}
        state = replace(unlisted, context=context)
        if not self.session.is_snowflake:
            return self._fakesnow_objects(state)
        if listed.objects:
            state = self._snowflake_objects(state)
        if listed.session:
            parameters = {str(p["key"]): (str(p["value"]), str(p["type"])) for p in self._show("parameters in session")}
            variables = {str(v["name"]): (str(v["value"]), str(v["type"])) for v in self._show("variables")}
            state = replace(state, parameters=parameters, variables=variables)
        return state

    def _snowflake_objects(self, state: State) -> State:
        # SHOW TERSE: metadata only, no warehouse, and a fraction of the time of an INFORMATION_SCHEMA query
        names = self.session.run_names
        schemas = {str(s["name"]) for s in self._show(f"terse schemas in database {names.database}")}
        # LIKE treats the run's underscores as wildcards: only what starts with the run's name is the run's
        found = self._show(f"terse databases like '{names.run}%'")
        databases = {str(d["name"]) for d in found if str(d["name"]).startswith(names.run)}
        tables = {
            (str(t["database_name"]), str(t["schema_name"]), str(t["name"]))
            for t in self._show(f"terse tables in database {names.database}")
            if str(t["kind"]).upper() == "TEMPORARY"
        }
        return replace(
            state,
            schemas=frozenset(s for s in schemas if not names.is_other_run(s)),
            databases=frozenset(databases),
            temporary_tables=frozenset(tables),
        )

    def _fakesnow_objects(self, state: State) -> State:
        names = self.session.run_names
        cur = self.session.conn.cursor()
        cur.execute(
            f"select 'schema', schema_name from {names.database}.information_schema.schemata union all "
            f"select 'database', database_name from {names.database}.information_schema.databases "
            f"where startswith(database_name, '{names.run}')"
        )
        found: dict[str, set[str]] = {"schema": set(), "database": set()}
        for kind, name in cur.fetchall():
            if not names.is_other_run(str(name)):
                found[str(kind)].add(str(name))
        tables = {(*owner, name) for name, owner in self.session.temporary_tables().items() if owner}
        return replace(
            state,
            schemas=frozenset(found["schema"]),
            databases=frozenset(found["database"]),
            temporary_tables=frozenset(tables),
        )

    def changes(self, before: State, after: State) -> Iterator[Leak]:
        """The leaks between two snapshots, each with how to undo it."""
        names = self.session.run_names
        snowflake = self.session.is_snowflake

        def mine(schema: str) -> bool:
            # On Snowflake, only what's named after the run is certainly the run's to drop: the database is shared.
            return not snowflake or schema.startswith(names.run)

        for name in sorted(after.schemas - before.schemas):
            where = f"{names.database}.{name}"
            undo = f'drop schema if exists {names.database}."{_quoted(name)}" cascade' if mine(name) else None
            yield Leak(f"schema {where} left behind" + ("" if mine(name) else ", not named after the run"), undo)
        for name in sorted(before.schemas - after.schemas):
            yield Leak(f"schema {names.database}.{name} dropped", None)
        for name in sorted(after.databases - before.databases):
            yield Leak(f"database {name} left behind", f'drop database if exists "{_quoted(name)}" cascade')
        for name in sorted(before.databases - after.databases):
            yield Leak(f"database {name} dropped", None)
        for database, schema, name in sorted(after.temporary_tables - before.temporary_tables):
            # Snowflake drops a permanent table of the same name if the temporary one is already gone: only in the
            # run's schemas. fakesnow keeps temporary tables in temp.main, which holds nothing else.
            if snowflake:
                where = f'"{_quoted(database)}"."{_quoted(schema)}"."{_quoted(name)}"'
                undo = f"drop table if exists {where}" if mine(schema) else None
            else:
                undo = f'drop table if exists temp.main."{_quoted(name)}"'
            yield Leak(f"temporary table {database}.{schema}.{name} left behind", undo)
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
        if before.variables is not None and after.variables is not None:
            yield from _variable_changes(before.variables, after.variables)

    def _undo(self, leak: Leak) -> str:
        if leak.undo is None:
            return f"{leak.description} (not undone)"
        try:
            self.session.conn.cursor().execute(leak.undo)
        except snowflake.connector.errors.Error as e:
            # codes only: a pinned outcome shouldn't depend on the wording of a message
            return f"{leak.description} (undoing it failed: {e.errno} ({e.sqlstate}))"
        return leak.description

    def _show(self, what: str) -> list[dict[str, Any]]:
        """A SHOW's rows, by lower-case column name."""
        cur = self.session.conn.cursor()
        cur.execute(f"show {what}")
        columns = [d.name.lower() for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _variable_changes(before: Mapping[str, tuple[str, str]], after: Mapping[str, tuple[str, str]]) -> Iterator[Leak]:
    for name in sorted(after.keys() - before.keys()):
        yield Leak(f"session variable {name} set to {after[name][0]!r}", f"unset {_identifier(name)}")
    for name in sorted(before.keys() - after.keys()):
        yield Leak(f"session variable {name} unset", f"set {_identifier(name)} = {_literal(*before[name])}")
    for name in sorted(before.keys() & after.keys()):
        (old, kind), (new, _) = before[name], after[name]
        if new != old:
            undo = f"set {_identifier(name)} = {_literal(old, kind)}"
            yield Leak(f"session variable {name} changed from {old!r} to {new!r}", undo)


def _identifier(name: str) -> str:
    return name if re.fullmatch(r"[A-Z_][A-Z0-9_$]*", name) else f'"{_quoted(name)}"'


def _literal(value: str, kind: str) -> str:
    # SHOW VARIABLES lists values as text: numbers and booleans as written, anything else back as a string
    return value if kind.lower() in ("fixed", "real", "boolean") else f"'{_escaped(value)}'"


def _quoted(name: str) -> str:
    return name.replace('"', '""')


def _escaped(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
