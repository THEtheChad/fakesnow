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
- the main session's role and warehouse, as the connector reports them, and its secondary roles
  (CURRENT_SECONDARY_ROLES()): Snowflake only (fakesnow has none of them). No case may change any of these
  (loader.py rejects a statement that would); a change is a leak like any other.

The current database and schema aren't: the next case's CREATE SCHEMA makes its own schema current.

Each SHOW is a round trip to Snowflake, so there the snapshot lists only what the case could have changed, judged
from its statements by reach(); fakesnow's snapshot is in-process (or local) and lists everything after every case.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import snowflake.connector.errors

from probes.model import Case
from probes.outcome import Scrub
from probes.targets import Session, quoted

# The first keyword of a statement that can't change the session (its parameters, variables, autocommit or
# transaction) nor what the leak check lists: queries and DML, SHOW and DESCRIBE, stage file commands, COMMENT,
# COMMIT and ROLLBACK (which end a transaction, never start one), and USE of a database or schema (which the next
# case's CREATE SCHEMA resets). Not USE ROLE, SECONDARY ROLES or WAREHOUSE (_CONTEXT), nor a WITH that defines or
# calls a procedure (WITH p AS PROCEDURE ... CALL p()), which can run anything.
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
# what USE can change that no case may: the session's role, secondary roles and warehouse
_CONTEXT = frozenset({"role", "secondary", "warehouse"})
_RUNS_CODE = frozenset({"procedure", "call"})

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
    return statements_reach((*case.setup, *case.statements, *case.teardown))


def statements_reach(statements: Sequence[str]) -> Reach:
    """What these statements could change, wherever in a case they run (setup, statements or teardown)."""
    objects = False
    for sql in statements:
        words = _words(sql)
        if (
            words is None
            or (words[0] not in _CANT_CHANGE and words[0] not in _DDL)
            or (words[0] == "use" and _CONTEXT.intersection(words[1:2]))
            or (words[0] == "with" and not _RUNS_CODE.isdisjoint(words))
        ):
            return EVERYTHING
        objects |= words[0] in _DDL and not _LISTED_KINDS.isdisjoint(words)
    return Reach(session=False, objects=objects)


def changes_context(sql: str) -> bool:
    """Whether any statement in sql is USE ROLE, USE SECONDARY ROLES or USE WAREHOUSE, outside comments and quotes.

    Only what's written as a statement: EXECUTE IMMEDIATE or a procedure could still run one (and could change
    anything, by reach()).
    """
    first: list[str] = []
    for token in _tokens(sql):
        if token["unterminated"]:
            break
        if token["other"] == ";":
            first = []
        elif len(first) < 2:
            first.append((token["word"] or token[0]).lower())
            if first[0] == "use" and first[1:2] and first[1] in _CONTEXT:
                return True
    return False


def _tokens(sql: str) -> Iterator[re.Match[str]]:
    """The tokens of sql, but comments."""
    for token in _TOKEN.finditer(sql.replace("{database}", "D").replace("{schema}", "S")):
        if not token["comment"]:
            yield token


def _words(sql: str) -> list[str] | None:
    """A statement's words, lower case, outside comments and quotes; None unless it's plainly one statement."""
    words: list[str] = []
    ended = False
    for token in _tokens(sql):
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
    """The main session's, as (database, schema, name). On fakesnow, also those whose schema is gone (see
    Session.temporary_tables), which on Snowflake went with it."""
    context: Mapping[str, str | None] = field(default_factory=dict[str, str | None])
    """The main session's role, warehouse and (on Snowflake) secondary roles."""
    parameters: Mapping[str, Parameter] | None = None
    """Session parameters by name; None when not listed."""
    variables: Mapping[str, tuple[str | None, str]] | None = None
    """Session variable -> (value, type), the value None for SQL NULL; None when not listed."""


@dataclass(frozen=True)
class Parameter:
    """A session parameter as SHOW PARAMETERS lists it."""

    value: str | None
    type: str
    level: str
    """Where its value is set: SESSION, or else it's inherited (USER, ACCOUNT, or empty for the default)."""


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
            return (
                "schemas, databases, temporary tables, session parameters and variables, role, secondary roles and "
                "warehouse"
            )
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
        connector = {"role": getattr(conn, "role", None), "warehouse": getattr(conn, "warehouse", None)}
        context = {**unlisted.context, **connector}
        state = replace(unlisted, context=context)
        if not self.session.is_snowflake:
            return self._fakesnow_objects(state)
        if listed.objects:
            state = self._snowflake_objects(state)
        if listed.session:
            parameters = {
                str(p["key"]): Parameter(_text(p["value"]), str(p["type"]), str(p["level"] or ""))
                for p in self._show("parameters in session")
            }
            variables = {str(v["name"]): (_text(v["value"]), str(v["type"])) for v in self._show("variables")}
            context = {**context, "secondary roles": self._secondary_roles()}
            state = replace(state, parameters=parameters, variables=variables, context=context)
        return state

    def _secondary_roles(self) -> str | None:
        """CURRENT_SECONDARY_ROLES(), a JSON object, as text: a context function, which needs no warehouse."""
        cur = self.session.conn.cursor()
        try:
            cur.execute("select current_secondary_roles()")
        except snowflake.connector.errors.Error as e:
            return f"unknown ({e.errno} ({e.sqlstate}))"
        row = cur.fetchone()
        return _text(row[0]) if row else None

    def _snowflake_objects(self, state: State) -> State:
        # SHOW TERSE: metadata only, no warehouse, and a fraction of the time of an INFORMATION_SCHEMA query
        names = self.session.run_names
        schemas = {str(s["name"]) for s in self._show(f"terse schemas in database {names.database}")}
        # LIKE treats the run's underscores as wildcards: only what starts with the run's name is the run's
        found = self._show(f"terse databases like '{names.run}%'")
        databases = {str(d["name"]) for d in found if str(d["name"]).startswith(names.run)}
        # not TERSE: its kind is TABLE for a temporary table too; only the full listing says TEMPORARY
        tables = {
            (str(t["database_name"]), str(t["schema_name"]), str(t["name"]))
            for t in self._show(f"tables in database {names.database}")
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
        # one fakesnow didn't record is in DuckDB's temp catalog, as far as anyone can tell
        tables = {(*(owner or ("temp", "main")), name) for name, owner in self.session.temporary_tables().items()}
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
            undo = f"drop schema if exists {names.database}.{quoted(name)} cascade" if mine(name) else None
            yield Leak(f"schema {where} left behind" + ("" if mine(name) else ", not named after the run"), undo)
        for name in sorted(before.schemas - after.schemas):
            yield Leak(f"schema {names.database}.{name} dropped", None)
        for name in sorted(after.databases - before.databases):
            yield Leak(f"database {name} left behind", f"drop database if exists {quoted(name)} cascade")
        for name in sorted(before.databases - after.databases):
            yield Leak(f"database {name} dropped", None)
        for database, schema, name in sorted(after.temporary_tables - before.temporary_tables):
            # Snowflake drops a permanent table of the same name if the temporary one is already gone: only in the
            # run's schemas. fakesnow keeps temporary tables in temp.main, which holds nothing else.
            if snowflake:
                where = f"{quoted(database)}.{quoted(schema)}.{quoted(name)}"
                undo = f"drop table if exists {where}" if mine(schema) else None
            else:
                undo = f"drop table if exists temp.main.{quoted(name)}"
            yield Leak(f"temporary table {database}.{schema}.{name} left behind", undo)
        for key, old in before.context.items():
            new = after.context.get(key)
            if new != old:
                yield Leak(f"session {key} changed from {old} to {new}", _context_undo(key, old))
        old_parameters, new_parameters = before.parameters or {}, after.parameters or {}
        for key in sorted(old_parameters.keys() & new_parameters.keys()):
            old, new = old_parameters[key], new_parameters[key]
            if (new.value, new.type) != (old.value, old.type):
                description = f"session parameter {key} changed from {_shown(old.value, old.type, new.type)} to "
                yield Leak(description + _shown(new.value, new.type, old.type), _parameter_undo(key, old))
        if before.variables is not None and after.variables is not None:
            yield from _variable_changes(before.variables, after.variables)

    def _undo(self, leak: Leak) -> str:
        if leak.undo is None:
            return f"{leak.description} (not undone)"
        try:
            self.session.conn.cursor().execute(leak.undo)
        except Exception as e:  # in-process, fakesnow can raise more than the connector's errors
            # codes only: a pinned outcome shouldn't depend on the wording of a message
            code = f"{e.errno} ({e.sqlstate})" if isinstance(e, snowflake.connector.errors.Error) else type(e).__name__
            return f"{leak.description} (undoing it failed: {code})"
        return leak.description

    def _show(self, what: str) -> list[dict[str, Any]]:
        """A SHOW's rows, by lower-case column name."""
        cur = self.session.conn.cursor()
        cur.execute(f"show {what}")
        columns = [d.name.lower() for d in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _variable_changes(
    before: Mapping[str, tuple[str | None, str]], after: Mapping[str, tuple[str | None, str]]
) -> Iterator[Leak]:
    for name in sorted(after.keys() - before.keys()):
        yield Leak(f"session variable {name} set to {after[name][0]!r}", f"unset {_identifier(name)}")
    for name in sorted(before.keys() - after.keys()):
        value = _variable_literal(*before[name])
        yield Leak(f"session variable {name} unset", value and f"set {_identifier(name)} = {value}")
    for name in sorted(before.keys() & after.keys()):
        (old, old_type), (new, new_type) = before[name], after[name]
        if (new, new_type) != (old, old_type):
            value = _variable_literal(old, old_type)
            description = f"session variable {name} changed from {_shown(old, old_type, new_type)} to "
            yield Leak(description + _shown(new, new_type, old_type), value and f"set {_identifier(name)} = {value}")


def _shown(value: str | None, kind: str, other_kind: str) -> str:
    """A value for a leak's description, with its type when that's what changed."""
    return repr(value) if kind == other_kind else f"{value!r} ({kind})"


def _context_undo(key: str, old: str | None) -> str | None:
    if key != "secondary roles":
        return f"use {key} {quoted(old)}" if old else None
    # the session's own setting back, whatever the user's default: ALL or NONE; anything else is only reported
    try:
        roles: Any = json.loads(old or "")
    except ValueError:
        return None
    if not isinstance(roles, dict):
        return None
    value, listed = str(roles.get("value", "")), str(roles.get("roles", ""))
    if value.upper() == "ALL":
        return "use secondary roles all"
    return "use secondary roles none" if (value, listed) == ("", "") else None


def _parameter_undo(key: str, old: Parameter) -> str | None:
    if old.level.upper() != "SESSION":
        # inherited (from the user, the account or the default): unsetting it inherits it again
        return f"alter session unset {key}"
    value = _literal(old.value, old.type)
    return value and f"alter session set {key} = {value}"


def _variable_literal(value: str | None, kind: str) -> str | None:
    """A variable's value as SQL of its type (SQL NULL too); None when unsure, so it's reported, not undone."""
    if value is None:
        sql_type = _NULL_TYPES.get(kind.lower())
        return sql_type and f"cast(null as {sql_type})"
    return _literal(value, kind)


# SHOW VARIABLES' types, and the SQL type of a NULL of each
_NULL_TYPES = {"fixed": "number", "real": "double", "boolean": "boolean", "text": "varchar"}


def _literal(value: str | None, kind: str) -> str | None:
    """A non-NULL value as SHOW VARIABLES (fixed, real, boolean, text) or SHOW PARAMETERS (NUMBER, BOOLEAN,
    STRING) lists it, as a literal of the same type; None for NULL, another type, or a value unlike its type."""
    kind = kind.lower()
    if value is None:
        return None
    if kind in ("fixed", "number"):
        return value if re.fullmatch(r"-?\d+(\.\d+)?", value) else None
    if kind == "real":
        return f"cast('{_escaped(value)}' as double)" if re.fullmatch(r"[-+0-9.eE]+|-?inf|nan", value, re.I) else None
    if kind == "boolean":
        return value.lower() if value.lower() in ("true", "false") else None
    if kind in ("text", "string"):
        return f"'{_escaped(value)}'"
    return None


def _identifier(name: str) -> str:
    return name if re.fullmatch(r"[A-Z_][A-Z0-9_$]*", name) else quoted(name)


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _escaped(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
