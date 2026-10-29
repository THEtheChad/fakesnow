"""Where cases run: real Snowflake, fakesnow in-process (fakesnow.patch) or fakesnow's server.

Each target opens a session in the run's own schema, created at the start and dropped at the end, along with
every schema or database named after it: each case's own schema, and anything a case creates outside it.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import snowflake.connector
from snowflake.connector import SnowflakeConnection

import fakesnow
from fakesnow.instance import FakeSnow
from probes.outcome import ErrorInfo

if TYPE_CHECKING:
    from fakesnow.conn import FakeSnowflakeConnection

# fakesnow.server() imports the fakesnow.server module, which then shadows the function on the package, so a
# second call in the same process fails: keep the function from before that happens
_start_server = fakesnow.server

SNOWFLAKE = "snowflake"
FAKESNOW = "fakesnow"
FAKESNOW_SERVER = "fakesnow-server"
TARGETS = (SNOWFLAKE, FAKESNOW, FAKESNOW_SERVER)

ENV_PREFIX = "PROBES_SNOWFLAKE_"
# connector parameter for each environment variable, after the prefix
_ENV_PARAMS = {
    "CONNECTION_NAME": "connection_name",
    "ACCOUNT": "account",
    "USER": "user",
    "PASSWORD": "password",
    "AUTHENTICATOR": "authenticator",
    "PRIVATE_KEY_FILE": "private_key_file",
    "PRIVATE_KEY_FILE_PWD": "private_key_file_pwd",
    "ROLE": "role",
    "WAREHOUSE": "warehouse",
    "HOST": "host",
}

# applied to every Snowflake session the run opens
SNOWFLAKE_SESSION_PARAMETERS: dict[str, str | int | bool] = {
    "TIMEZONE": "UTC",
    # a probe is a handful of rows: anything slower is a mistake, so stop it before it costs anything
    "STATEMENT_TIMEOUT_IN_SECONDS": 60,
    "QUERY_TAG": "fakesnow-probes",
}


class TargetError(RuntimeError):
    """A target can't be used, e.g. Snowflake isn't configured."""


@dataclass(frozen=True)
class Names:
    """The run's database and unique schema, or a case's own schema within the run, and how cases refer to them."""

    database: str
    schema: str
    run_schema: str = ""
    """For a case's names: the run's schema, which the case's is named after. Empty for the run's own names."""

    @classmethod
    def new(cls, database: str) -> Names:
        return cls(database, f"PROBES_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(8).upper()}")

    def __post_init__(self) -> None:
        for name in filter(None, (self.database, self.schema, self.run_schema)):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
                raise TargetError("probe database/schema names must be simple unquoted SQL identifiers")
        for name in ("database", "schema", "run_schema"):
            object.__setattr__(self, name, getattr(self, name).upper())

    @property
    def run(self) -> str:
        """The run's schema: every schema or database the run creates is named starting with it."""
        return self.run_schema or self.schema

    def case(self, case_id: str) -> Names:
        """A case's names: its own schema, named after the run's so cleanup finds it.

        The suffix comes from the case's id, so the name is the same in every order, selection and run, and as long
        as any other case's: a case that shows its schema's name (say, its length) can't depend on which cases ran.
        """
        return Names(self.database, f"{self.run}_C{hashlib.sha256(case_id.encode()).hexdigest()[:8].upper()}", self.run)

    def render(self, sql: str) -> str:
        return sql.replace("{database}", self.database).replace("{schema}", self.schema)

    @property
    def placeholders(self) -> dict[str, str]:
        # a case's schema starts with the run's: the scrubber tries the longer name first
        run = {self.run_schema: "{run_schema}"} if self.run_schema else {}
        return {self.schema: "{schema}", self.database: "{database}", **run}

    def is_other_run(self, name: str) -> bool:
        """Whether a schema or database is named like another run's: concurrent runs may share a database."""
        return bool(_RUN_NAME.match(name)) and not name.upper().startswith(self.run)


_RUN_NAME = re.compile(r"PROBES_\d{8}_\d{6}_[0-9A-F]{16}", re.IGNORECASE)


def quoted(name: str) -> str:
    """A name as a quoted identifier: every identifier the runner generates from a name it found goes through this."""
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class SnowflakeConfig:
    """How to connect to Snowflake, read from PROBES_SNOWFLAKE_* environment variables.

    Kept out of reprs and reports: params can hold a password or a key's passphrase.
    """

    database: str
    params: Mapping[str, str] = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> SnowflakeConfig | None:
        """The configuration, or None when none is given: the suite never connects to an account by default."""
        params = {p: env[ENV_PREFIX + k] for k, p in _ENV_PARAMS.items() if env.get(ENV_PREFIX + k)}
        if not (params.get("account") or params.get("connection_name")):
            return None
        database = env.get(ENV_PREFIX + "DATABASE")
        if not database:
            raise TargetError(f"set {ENV_PREFIX}DATABASE: the database the run creates its schema in")
        if "private_key_file" in params:
            params.setdefault("authenticator", "SNOWFLAKE_JWT")
        return cls(database, params)

    def describe(self) -> str:
        """A summary safe to print: no account, password or key."""
        auth = "key pair" if "private_key_file" in self.params else self.params.get("authenticator", "password")
        parts = [f"database {self.database}", f"auth {auth}"]
        keys = ("connection_name", "user", "role", "warehouse")
        parts += [f"{k.replace('_', ' ')} {self.params[k]}" for k in keys if k in self.params]
        return ", ".join(parts)


@dataclass
class Session:
    """An open target: its main connection, and a way to open more sessions in the run's database.

    Each case runs in a schema of its own (enter_case and leave_case), so what one case creates in its schema can't
    affect another.
    """

    target: str
    run_names: Names
    conn: SnowflakeConnection
    open_connection: Callable[[str], SnowflakeConnection]
    """Opens another session in the given schema of the run's database."""
    info: dict[str, str]
    fakesnow_session: Callable[[], FakeSnowflakeConnection] | None = None
    """fakesnow's own object for the main session, for what SQL can't tell (see temporary_tables)."""
    names: Names = field(init=False)
    """The current case's names, whose {schema} is the case's own schema; the run's between cases."""
    _fakesnow_own: frozenset[int] = field(init=False, default=frozenset())
    """fakesnow only: the OIDs of its own temporary tables, which it creates as the session opens."""

    def __post_init__(self) -> None:
        self.names = self.run_names
        if self.fakesnow_session:
            self._fakesnow_own = frozenset(self._temporary_oids().values())

    @property
    def is_snowflake(self) -> bool:
        return self.target == SNOWFLAKE

    def connect(self) -> SnowflakeConnection:
        return self.open_connection(self.names.schema)

    def enter_case(self, case_id: str) -> Names:
        """Create the case's schema, which makes it the main session's current schema."""
        names = self.run_names.case(case_id)
        self.conn.cursor().execute(f"create schema {names.database}.{names.schema}")
        self.names = names
        self.restore()
        return names

    def leave_case(self) -> str | None:
        """Drop the case's schema and everything in it; why that failed, if it did (the leak check then reports it).

        On fakesnow, also drop the temporary tables created in the case's schema, as dropping it does on Snowflake:
        fakesnow keeps them (case ddl.drop-schema-drops-temporary-table), and they'd shadow later cases' tables.
        Only those: any other temporary table, its schema dropped or not, is the leak check's to report (and, unless
        leaks are kept, undo).
        """
        names, self.names = self.names, self.run_names
        if names is self.run_names:
            return None
        try:
            self.conn.cursor().execute(f"drop schema if exists {names.database}.{names.schema} cascade")
        except snowflake.connector.errors.Error as e:
            return f"dropping the case's schema failed: {ErrorInfo.from_exception(e)}"
        if not self.fakesnow_session:
            return None
        failed: list[str] = []
        for name, owner in self.temporary_tables().items():
            if owner == (names.database, names.schema):
                try:
                    self.conn.cursor().execute(f"drop table if exists temp.main.{quoted(name)}")
                except Exception as e:  # in-process, fakesnow can raise more than the connector's errors
                    failed.append(f"{name}: {ErrorInfo.from_exception(e)}")
        return f"dropping the case's temporary tables failed: {'; '.join(failed)}" if failed else None

    def temporary_tables(self) -> dict[str, tuple[str, str] | None]:
        """fakesnow only: the main session's temporary tables, each with the database and schema it was created in,
        even if that's since been dropped; None if fakesnow didn't record it.

        DuckDB keeps every temporary table in one catalog (temp.main), so only fakesnow knows each one's schema.
        Not fakesnow's own (its column metadata, temp.main._fs_columns_ext): those it had before the run's first
        statement, known by OID, which DuckDB never reuses, rather than by name, which a case can share.
        """
        assert self.fakesnow_session, "Snowflake lists temporary tables with SHOW TABLES"
        owners = self.fakesnow_session().temporary_tables.owners()
        tables = [name for name, oid in self._temporary_oids().items() if oid not in self._fakesnow_own]
        return {table: _owner(owners.get(table)) for table in tables}

    def _temporary_oids(self) -> dict[str, int]:
        """fakesnow only: the main session's temporary tables' OIDs, by name (unique among them)."""
        cur = self.conn.cursor()
        # DuckDB's catalog function, which fakesnow passes through
        cur.execute("select table_name, table_oid from duckdb_tables() where temporary")
        return {str(name): int(oid) for name, oid in cur.fetchall()}

    def restore(self) -> None:
        """Put the main session back in the current case's schema, if the connector reports it elsewhere."""
        if (self.conn.database or "").upper() != self.names.database.upper() or (
            self.conn.schema or ""
        ).upper() != self.names.schema.upper():
            self.conn.cursor().execute(f"use schema {self.names.database}.{self.names.schema}")


def _owner(owner: tuple[str | None, str | None] | None) -> tuple[str, str] | None:
    return (str(owner[0]), str(owner[1])) if owner else None


@contextmanager
def open_target(target: str, names: Names, snowflake_config: SnowflakeConfig | None = None) -> Iterator[Session]:
    if target == SNOWFLAKE:
        if not snowflake_config:
            raise TargetError(
                f"Snowflake isn't configured: set {ENV_PREFIX}ACCOUNT (or {ENV_PREFIX}CONNECTION_NAME), "
                f"{ENV_PREFIX}USER, {ENV_PREFIX}DATABASE and credentials; see probes/README.md"
            )
        with _snowflake(names, snowflake_config) as session:
            yield session
    elif target == FAKESNOW:
        with _fakesnow(names) as session:
            yield session
    elif target == FAKESNOW_SERVER:
        with _fakesnow_server(names) as session:
            yield session
    else:
        raise TargetError(f"unknown target {target!r}, expected one of {', '.join(TARGETS)}")


@contextmanager
def _snowflake(names: Names, config: SnowflakeConfig) -> Iterator[Session]:
    def connect(schema: str | None = None) -> SnowflakeConnection:
        return snowflake.connector.connect(
            **config.params,
            database=names.database,
            **({"schema": schema} if schema else {}),
            session_parameters=dict(SNOWFLAKE_SESSION_PARAMETERS),
            login_timeout=30,
            network_timeout=60,
        )

    conn = connect()
    cleanup_needed = False
    try:
        cur = conn.cursor()
        cur.execute("select current_version()")
        row = cur.fetchone()
        version = str(row[0]) if row else "?"
        # A cancelled request can have created the schema before the connector reports success.
        cleanup_needed = True
        cur.execute(f"create schema {names.database}.{names.schema}")
        yield Session(
            SNOWFLAKE,
            names,
            conn,
            connect,
            {"version": version, "connector": snowflake.connector.__version__},
        )
    finally:
        try:
            # Release transaction locks before dropping objects, even if reset/rollback failed.
            conn.close()
        finally:
            if cleanup_needed:
                # The probe may close/break its connection. Clean up through an independent session.
                cleanup = connect()
                try:
                    _drop_run_objects(cleanup, names)
                finally:
                    cleanup.close()


def _drop_run_objects(conn: SnowflakeConnection, names: Names) -> None:
    """Drop the run's schema, and any schema (or database) a case created named after it."""
    cur = conn.cursor()
    statements = [f"drop schema if exists {names.database}.{names.schema} cascade"]
    for kind, scope in [("schemas", f" in database {names.database}"), ("databases", "")]:
        try:
            cur.execute(f"show {kind} like '{names.schema}%'{scope}")
            found = [r[1] for r in cur.fetchall()]
        except snowflake.connector.errors.Error as e:
            print(f"warning: couldn't list {kind} to clean up: {e.errno} {e.sqlstate}", file=sys.stderr)
            continue
        for name in found:
            # SHOW LIKE treats underscores in the generated prefix as wildcards. Never delete a
            # different run's objects merely because they match that SQL pattern.
            if not str(name).startswith(names.schema):
                continue
            where = f"{names.database}.{quoted(str(name))}" if kind == "schemas" else quoted(str(name))
            statements.append(f"drop {kind[:-1]} if exists {where} cascade")
    for sql in dict.fromkeys(statements):
        try:
            cur.execute(sql)
        except snowflake.connector.errors.Error as e:
            print(f"warning: cleanup failed: {sql}: {e.errno} {e.sqlstate}", file=sys.stderr)


@contextmanager
def _fakesnow(names: Names) -> Iterator[Session]:
    with fakesnow.patch():
        conn = snowflake.connector.connect(database=names.database)
        try:
            conn.cursor().execute(f"create schema {names.database}.{names.schema}")
            fake: Any = conn  # fakesnow.patch makes connect return fakesnow's connection
            yield Session(
                FAKESNOW,
                names,
                conn,
                lambda schema: snowflake.connector.connect(database=names.database, schema=schema),
                fakesnow_info(),
                lambda: fake,
            )
        finally:
            conn.close()


@contextmanager
def _fakesnow_server(names: Names) -> Iterator[Session]:
    if not callable(_start_server):
        raise TargetError("fakesnow.server was imported as a module before probes: start probes first")
    # uvicorn configures its logging as it starts, so it's quietened after (only its start-up lines get through)
    with (
        _start_server() as server_kwargs,
        _fresh_server_instance() as server,
        _quiet("fakesnow.server", "uvicorn", "uvicorn.access", "uvicorn.error"),
    ):

        def connect(schema: str | None = None) -> SnowflakeConnection:
            # the connector changes session_parameters in place, so each connection gets its own copy
            kwargs: dict[str, Any] = copy.deepcopy(server_kwargs)
            return snowflake.connector.connect(
                **kwargs, database=names.database, **({"schema": schema} if schema else {})
            )

        conn = connect()
        try:
            conn.cursor().execute(f"create schema {names.database}.{names.schema}")
            token = conn.rest.token if conn.rest else None
            yield Session(FAKESNOW_SERVER, names, conn, connect, fakesnow_info(), lambda: server.sessions[str(token)])
        finally:
            conn.close()


@contextmanager
def _fresh_server_instance() -> Iterator[Any]:
    """Serve logins from a fakesnow instance of the target's own while it's open, and yield the server module.

    Every server in a process serves the same instance (fakesnow.server.shared_fs), so what one run leaves there,
    such as a database (fakesnow can't drop one), would reach the next: a --find-polluter trial, say. On the way out
    the instance's sessions are closed and forgotten (the server keeps every session it hasn't been told to delete,
    and each one keeps its instance's result cache), and no other.

    Not reentrant: shared_fs is one module global, so while two of these overlap (other than nested), logins go to
    whichever swapped in last, and the first to leave puts back an instance the other then serves. Targets run one
    at a time.
    """
    server: Any = sys.modules["fakesnow.server"]
    instance = FakeSnow()
    shared, server.shared_fs = server.shared_fs, instance
    try:
        yield server
    finally:
        server.shared_fs = shared
        sessions: dict[str, Any] = server.sessions
        for token, conn in list(sessions.items()):
            if conn.results_cache is instance.results_cache:
                del sessions[token]
                conn.close()
        instance.results_cache.clear()
        instance.duck_conn.close()


@contextmanager
def _quiet(*loggers: str) -> Iterator[None]:
    """The server logs every request, and every failure with a traceback: the outcomes already hold what failed."""
    levels = {name: logging.getLogger(name).level for name in loggers}
    for name in loggers:
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


def fakesnow_info() -> dict[str, str]:
    import duckdb

    info = {"version": importlib.metadata.version("fakesnow")}
    source = Path(fakesnow.__file__).parent
    try:
        commit = subprocess.run(
            ["git", "-C", str(source), "describe", "--always", "--dirty", "--exclude", "*"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        if re.fullmatch(r"[0-9a-f]+(-dirty)?", commit):
            info["commit"] = commit
    except (OSError, subprocess.SubprocessError):
        pass
    info["duckdb"] = duckdb.__version__
    info["sqlglot"] = importlib.metadata.version("sqlglot")
    return info
