"""Isolation: each case's own schema, the leak check, shuffled order, and finding polluters. No Snowflake."""

import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fakesnow.instance import FakeSnow
from probes import targets
from probes.cli import main, run
from probes.compare import Status, classify, comparison_fingerprint
from probes.isolation import EVERYTHING, LeakCheck, Parameter, Reach, State, changes_context, reach
from probes.loader import LoadError, load_cases
from probes.model import Case, Expected
from probes.outcome import Column, Outcome, ResultSet, Step, decode, encode, scrubber
from probes.polluter import ddmin, find_polluters
from probes.report import leak_scope, load_run, markdown, summary, to_json, write_json
from probes.runner import compare_runs, run_case, run_target, shuffled
from probes.targets import FAKESNOW, FAKESNOW_SERVER, SNOWFLAKE, Names, Session, TargetError, open_target

CASES = """
from probes import Case

def other_session(conn, ctx):
    return ctx.query(ctx.connect().cursor(), "select current_schema()")

CASES = [
    Case(
        id="iso.create",
        description="creates a table and a schema named after its own, and says where it is",
        sql=[
            "create table iso_t (x int)",
            "select current_schema(), '{schema}_S2', lower('{schema}')",
            "create schema {database}.{schema}_S2",
        ],
        teardown=["drop schema if exists {database}.{schema}_S2"],
    ),
    Case(id="iso.read", description="can't see the table the other case created", sql="select * from iso_t"),
    Case(id="iso.session", description="another session opens in the case's schema", run=other_session),
]
"""

LEAKS = """
from probes import Case

CASES = [
    Case(id="leak.schema", description="leaves a schema behind", sql="create schema {database}.{schema}_L"),
    Case(id="leak.temporary", description="leaves a temporary table", sql="create temporary table leak_tt (x int)"),
    Case(id="leak.after", description="the same temporary table again", sql="create temporary table leak_tt (x int)"),
]
"""

POLLUTERS = """
from probes import Case

CASES = [
    Case(id="p.a", description="unrelated", sql="select 1"),
    Case(id="p.x", description="sets a session variable", sql="set p_x = 1"),
    Case(id="p.b", description="unrelated", sql="create table p_t (x int)"),
    Case(id="p.y", description="sets another", sql="set p_y = 2"),
    Case(id="p.c", description="unrelated", sql="select 2"),
    Case(id="p.schema", description="leaves a schema behind", sql="create schema {database}.{schema}_P"),
    Case(id="p.victim", description="reads both variables", sql="select $p_x + $p_y"),
    Case(
        id="p.lister",
        description="counts schemas left behind",
        sql="select count(*) from information_schema.schemata where endswith(schema_name, '_P')",
    ),
]
"""


def case_dir(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    (path / f"{name}.py").write_text(source)
    return path


def select(case_id: str, sql: str = "select 1") -> Case:
    return Case(id=case_id, description="d", sql=sql)


# --- each case's own schema


def test_a_case_schema_is_the_runs_name_with_a_suffix_from_the_case_id():
    run = Names("db", "probes_x")
    case = run.case("a.case")
    assert (case.database, case.run, run.run) == ("DB", "PROBES_X", "PROBES_X")
    assert re.fullmatch(r"PROBES_X_C[0-9A-F]{8}", case.schema)
    # the same in every run, order and selection, and as long as any other case's
    assert Names("db", "probes_x").case("a.case") == case
    assert case.schema != run.case("another.case").schema
    assert len({len(run.case(f"case.{i}").schema) for i in range(20)}) == 1


def test_a_case_schema_renders_and_scrubs_to_schema():
    case = Names("db", "probes_x").case("a.case")
    schema = case.schema
    assert case.render("{database}.{schema}_S2") == f"DB.{schema}_S2"
    scrub = scrubber(case.placeholders)
    assert scrub(f"{schema} {schema}_S2 {schema.lower()} DB") == "{schema} {schema}_S2 {schema:lower} {database}"
    # another case's schema, or the run's own, is never this case's {schema}
    other = Names("db", "probes_x").case("b.case").schema
    assert scrub(f"{other} PROBES_X") == f"{{run_schema}}{other.removeprefix('PROBES_X')} {{run_schema}}"


def test_names_recognise_other_runs():
    names = Names("DB", "PROBES_20261029_101500_0123456789ABCDEF")
    assert names.is_other_run("PROBES_20261029_101501_0123456789ABCDEF_C1")
    assert not names.is_other_run(names.schema + "_C1")
    assert not names.is_other_run("PUBLIC")


@pytest.mark.parametrize("target", [FAKESNOW, FAKESNOW_SERVER])
def test_each_case_runs_in_its_own_schema(tmp_path: Path, target: str):
    cases = load_cases([case_dir(tmp_path, "iso", CASES)])
    run = run_target(target, cases, Names.new("PROBES"))
    created = run.outcomes["iso.create"]
    assert created.steps[1].result
    assert created.steps[1].result.rows == (("{schema}", "{schema}_S2", "{schema:lower}"),)
    assert created.leaks == ()
    read = run.outcomes["iso.read"]
    assert read.steps[0].error
    assert read.steps[0].error.sqlstate == "42S02"
    session = run.outcomes["iso.session"].value
    assert isinstance(session, ResultSet)
    assert session.rows == (("{schema}",),)


def test_server_runs_in_one_process_dont_see_each_others_schemas():
    count = select("count", "select count(*) from information_schema.schemata")
    first = run_target(FAKESNOW_SERVER, [count], Names.new("PROBES")).outcomes["count"]
    assert run_target(FAKESNOW_SERVER, [count], Names.new("PROBES")).outcomes["count"] == first


def test_server_runs_in_one_process_dont_see_each_others_databases():
    # fakesnow can't drop a database: one a run leaves behind mustn't reach the next run
    count = select("count", "select count(*) from information_schema.databases")
    first = run_target(FAKESNOW_SERVER, [count], Names.new("PROBES")).outcomes["count"]
    run_target(FAKESNOW_SERVER, [select("creator", "create database {schema}_DB")], Names.new("PROBES"))
    assert run_target(FAKESNOW_SERVER, [count], Names.new("PROBES")).outcomes["count"] == first


def test_a_server_target_forgets_its_sessions_on_the_way_out_and_only_its_own():
    # probes keeps fakesnow.server() from before the module shadows it, so importing it here is safe
    server: Any = importlib.import_module("fakesnow.server")
    unrelated = FakeSnow().connect()  # another instance's session, as another server user's would be
    server.sessions["unrelated"] = unrelated

    def opens_another(conn: Any, ctx: Any) -> int:
        ctx.connect()
        return 1

    cases = [select("a"), Case(id="b", description="d", run=opens_another)]
    try:
        run_target(FAKESNOW_SERVER, cases, Names.new("PROBES"))
        assert server.sessions == {"unrelated": unrelated}

        def fails(target: str, case: Case) -> None:
            if case.id == "b":
                raise RuntimeError("interrupted")

        with pytest.raises(RuntimeError, match="interrupted"):
            run_target(FAKESNOW_SERVER, cases, Names.new("PROBES"), progress=fails)
        assert server.sessions == {"unrelated": unrelated}
    finally:
        server.sessions.pop("unrelated", None)


# --- the leak check on fakesnow


def test_leaks_are_reported_against_the_case_and_undone(tmp_path: Path):
    cases = load_cases([case_dir(tmp_path, "leak", LEAKS)])
    with open_target(FAKESNOW, Names.new("PROBES")) as session:
        check = LeakCheck(session)
        outcomes = [run_case(case, session, check) for case in cases]
        cur = session.conn.cursor()
        leaked = session.run_names.case("leak.schema").schema + "_L"
        cur.execute(f"select count(*) from information_schema.schemata where schema_name = '{leaked}'")
        assert cur.fetchall() == [(0,)]
    assert outcomes[0].leaks == ("schema {database}.{schema}_L left behind",)
    # a temporary table in the case's schema goes with it, as on Snowflake (fakesnow keeps it: the runner drops it)
    assert outcomes[1].leaks == ()
    assert outcomes[2].leaks == ()
    assert outcomes[2].steps[0].error is None


def test_keep_leaks_reports_each_once_and_leaves_it(tmp_path: Path):
    cases = load_cases([case_dir(tmp_path, "leak", LEAKS)])
    with open_target(FAKESNOW, Names.new("PROBES")) as session:
        check = LeakCheck(session, cleanup=False)
        first = run_case(cases[0], session, check)
        second = run_case(select("quiet"), session, check)
        cur = session.conn.cursor()
        leaked = session.run_names.case("leak.schema").schema + "_L"
        cur.execute(f"select count(*) from information_schema.schemata where schema_name = '{leaked}'")
        assert cur.fetchall() == [(1,)]
    assert first.leaks and not second.leaks


def temporary_in_run_schema(names: Names) -> Case:
    return select("home.temporary", f"create temporary table {{database}}.{names.schema}.home_tt (x int)")


@pytest.mark.parametrize("target", [FAKESNOW, FAKESNOW_SERVER])
def test_a_temporary_table_outside_the_case_schema_is_a_leak_and_undone(target: str):
    names = Names.new("PROBES")
    with open_target(target, names) as session:
        check = LeakCheck(session)
        outcome = run_case(temporary_in_run_schema(names), session, check)
        assert run_case(select("next", "select * from home_tt"), session, check).steps[0].error
    assert outcome.leaks == ("temporary table {database}.{run_schema}.HOME_TT left behind",)


def test_keep_leaks_leaves_a_temporary_table_outside_the_case_schema():
    names = Names.new("PROBES")
    with open_target(FAKESNOW, names) as session:
        check = LeakCheck(session, cleanup=False)
        outcome = run_case(temporary_in_run_schema(names), session, check)
        assert run_case(select("next", "select * from home_tt"), session, check).steps[0].error is None
    assert outcome.leaks == ("temporary table {database}.{run_schema}.HOME_TT left behind",)


DROPS_ITS_OTHER_SCHEMA = Case(
    id="dropped",
    description="d",
    setup=["create schema {database}.{schema}_T", "create temporary table {database}.{schema}_T.gone_tt (x int)"],
    sql="drop schema {database}.{schema}_T cascade",
)


@pytest.mark.parametrize("cleanup", [True, False], ids=["undone", "kept"])
def test_a_temporary_table_left_by_dropping_another_schema_is_a_fakesnow_leak(cleanup: bool):
    # on Snowflake the table goes with its schema; fakesnow keeps it: only the case's own schema's go silently
    with open_target(FAKESNOW, Names.new("PROBES")) as session:
        check = LeakCheck(session, cleanup=cleanup)
        outcome = run_case(DROPS_ITS_OTHER_SCHEMA, session, check)
        again = run_case(select("again", "create temporary table gone_tt (x int)"), session, check)
    assert outcome.leaks == ("temporary table {database}.{schema}_T.GONE_TT left behind",)
    assert (again.steps[0].error is None) == cleanup


def test_the_case_schemas_own_temporary_tables_go_with_it_silently():
    owned = select("owned", "create temporary table {database}.{schema}.own_tt (x int)")
    with open_target(FAKESNOW, Names.new("PROBES")) as session:
        check = LeakCheck(session, cleanup=False)
        assert run_case(owned, session, check).leaks == ()
        assert session.temporary_tables() == {}


def test_a_temporary_table_another_schema_owned_is_never_dropped_by_a_later_case():
    # the reviewer's repro: a temporary table in PROBES.OUTSIDE, then OUTSIDE dropped, then an unrelated case
    names = Names.new("PROBES")
    with open_target(FAKESNOW, names) as session:
        cur = session.conn.cursor()
        cur.execute(f"create schema {names.database}.OUTSIDE")
        cur.execute(f"create temporary table {names.database}.OUTSIDE.kept_tt (x int)")
        cur.execute(f"drop schema {names.database}.OUTSIDE")
        session.restore()
        check = LeakCheck(session, cleanup=False)
        outcome = run_case(select("unrelated"), session, check)
        assert session.temporary_tables() == {"KEPT_TT": (names.database, "OUTSIDE")}
    assert outcome.leaks == ()


@pytest.mark.parametrize("target", [FAKESNOW, FAKESNOW_SERVER])
def test_a_temporary_table_named_like_fakesnows_own_goes_with_the_case_schema(target: str):
    # the reviewer's repro: fakesnow's own are known by OID, not by an _fs_ prefix a case can use too
    fs_named = select("fs-named", 'create temporary table "_fs_user" (x int)')
    with open_target(target, Names.new("PROBES")) as session:
        check = LeakCheck(session)
        outcomes = [run_case(fs_named, session, check) for _ in range(2)]
        assert session.temporary_tables() == {}
    assert [(o.leaks, o.notes, o.steps[0].error) for o in outcomes] == [((), (), None)] * 2


@pytest.mark.parametrize("cleanup", [True, False], ids=["undone", "kept"])
@pytest.mark.parametrize("target", [FAKESNOW, FAKESNOW_SERVER])
def test_a_temporary_table_named_like_fakesnows_own_outside_the_case_schema_is_a_leak(target: str, cleanup: bool):
    names = Names.new("PROBES")
    fs_named = select("fs-named", f'create temporary table {{database}}.{names.schema}."_fs_user" (x int)')
    with open_target(target, names) as session:
        check = LeakCheck(session, cleanup=cleanup)
        outcome = run_case(fs_named, session, check)
        again = run_case(select("next", 'select * from "_fs_user"'), session, check)
    assert outcome.leaks == ("temporary table {database}.{run_schema}._fs_user left behind",)
    assert again.leaks == ()
    assert (again.steps[0].error is None) == (not cleanup)


def test_fakesnows_own_temporary_tables_arent_listed():
    with open_target(FAKESNOW, Names.new("PROBES")) as session:
        cur = session.conn.cursor()
        cur.execute("select table_name from duckdb_tables() where temporary")
        assert [row[0] for row in cur.fetchall()] == ["_fs_columns_ext"]
        assert session.temporary_tables() == {}


@pytest.mark.parametrize("target", [FAKESNOW, FAKESNOW_SERVER])
def test_a_temporary_table_with_a_quote_in_its_name_is_dropped_with_the_case(target: str):
    quoting = select("quoting", 'create temporary table "a""b" (x int)')
    with open_target(target, Names.new("PROBES")) as session:
        check = LeakCheck(session)
        outcome = run_case(quoting, session, check)
        again = run_case(quoting, session, check)
    assert (outcome.notes, outcome.leaks) == ((), ())
    assert again.steps[0].error is None


def test_a_failed_temporary_table_drop_is_a_note_and_keeps_the_outcome(monkeypatch: pytest.MonkeyPatch):
    # how names were quoted before: a quote in one broke fakesnow's tokenizer, and the case's outcome with it
    monkeypatch.setattr(targets, "quoted", lambda name: f'"{name}"')
    quoting = Case(id="quoting", description="d", sql=['create temporary table "a""b" (x int)', "select 1"])
    with open_target(FAKESNOW, Names.new("PROBES")) as session:
        check = LeakCheck(session)
        outcome = run_case(quoting, session, check)
        assert session.temporary_tables() == {}
    assert len(outcome.steps) == 2
    assert outcome.steps[1].result
    assert len(outcome.notes) == 1
    assert outcome.notes[0].startswith("dropping the case's temporary tables failed: a\"b: ")
    # the leak check finds it still there, and drops it with its name quoted properly
    assert outcome.leaks == ('temporary table {database}.{schema}.a"b left behind',)


AUTOCOMMIT_OFF = Case(
    id="autocommit-off",
    description="turns autocommit off in its teardown",
    sql="select 1",
    teardown=["alter session set autocommit = false"],
)
COUNTS_A_ROLLED_BACK_INSERT = Case(
    id="victim",
    description="a rolled-back insert leaves nothing",
    setup=["create table victim_t (x int)"],
    sql=["insert into victim_t values (1)", "rollback", "select count(*) from victim_t"],
)


@pytest.mark.parametrize("target", [FAKESNOW, FAKESNOW_SERVER])
def test_a_teardown_that_turns_autocommit_off_doesnt_reach_the_next_case(target: str):
    def count(cases: list[Case]) -> Any:
        outcome = run_target(target, cases, Names.new("PROBES")).outcomes["victim"]
        assert outcome.steps[2].result
        return outcome.steps[2].result.rows

    assert count([COUNTS_A_ROLLED_BACK_INSERT]) == ((1,),)
    assert count([AUTOCOMMIT_OFF, COUNTS_A_ROLLED_BACK_INSERT]) == ((1,),)


# --- what a case could change


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER /* c */ SESSION SET TIMEZONE = 'America/New_York'",
        "-- c\nalter session set timezone = 'UTC'",
        "alter // c\n session set timezone = 'UTC'",
        "alter table t add column y int",  # ALTER is never on the list: ALTER SESSION, USER, ACCOUNT
        "set reviewer_variable = 42",
        "unset reviewer_variable",
        "/* c */ begin",
        "call p()",
        "execute immediate 'select 1'",
        "with p as procedure () returns int language sql as $$ begin set x = 1; return 1; end $$ call p()",
        "WITH /* c */ p AS PROCEDURE () RETURNS INT LANGUAGE SQL AS 'begin return 1; end' CALL p()",
        "use role other_role",
        "use /* c */ secondary roles none",
        "USE WAREHOUSE other_wh",
        "select 1; alter session set timezone = 'UTC'",  # more than one statement
        "select 'unterminated",
        "/* unterminated select 1",
        "'text' select 1",  # anything but a keyword first
    ],
)
def test_anything_not_plainly_a_listed_kind_could_change_everything(sql: str):
    assert reach(select("a", sql)) == EVERYTHING


def test_a_python_probe_could_change_everything():
    assert reach(Case(id="a", description="d", run=lambda conn, ctx: None)) == EVERYTHING


@pytest.mark.parametrize(
    "sql",
    [
        "select 'alter session set timezone = ''UTC'''",
        "-- alter session\nselect 1",
        "/* set x = 1 */ select 1;",
        "(select 1) union all (select 2)",
        "with x as (select 1) select * from x",
        "insert into t values ('; alter session')",
        "show tables",
        "describe table t",
        "use schema {database}.{schema}",
        "create table t (x int)",
        "create table schema_t as select * from information_schema.tables",  # names, not the SCHEMA keyword
        "drop table t",
        "create procedure p() returns int language sql as $$ begin alter session set timezone = 'UTC'; end $$",
    ],
)
def test_listed_kinds_change_nothing_the_check_lists(sql: str):
    assert reach(select("a", sql)) == Reach(session=False, objects=False)


@pytest.mark.parametrize(
    "sql",
    [
        "create schema {database}.{schema}_S2",
        "drop database if exists {schema}_DB",
        "create or replace temporary table t (x int)",
        "create temp table t (x int)",
        "undrop schema x",
    ],
)
def test_ddl_on_schemas_databases_or_temporary_tables_changes_objects_only(sql: str):
    assert reach(select("a", sql)) == Reach(session=False, objects=True)


def test_every_statement_counts_setup_and_teardown_too():
    case = Case(id="a", description="d", setup=["create table t (x int)"], sql="select 1", teardown=["set x = 1"])
    assert reach(case) == EVERYTHING


@pytest.mark.parametrize(
    "sql",
    [
        "use role other_role",
        "USE SECONDARY ROLES NONE",
        "use /* c */ secondary roles all",
        "-- c\nuse warehouse other_wh",
        "select 1; use role other_role",
    ],
)
@pytest.mark.parametrize("where", ["setup", "sql", "teardown"])
def test_cases_that_change_role_secondary_roles_or_warehouse_dont_load(tmp_path: Path, sql: str, where: str):
    fields = {"sql": ["select 1"], where: [sql]}
    source = f"from probes import Case\nCASES = [Case(id='a', description='d', **{fields!r})]"
    with pytest.raises(LoadError, match="changes the session's role, secondary roles or warehouse"):
        load_cases([case_dir(tmp_path, "ctx", source)])


@pytest.mark.parametrize(
    "sql",
    ["use schema {database}.{schema}", "use database x", "select 'use role x'", "-- use role x\nselect 1", "use x"],
)
def test_using_a_database_or_schema_or_quoting_use_role_loads(sql: str):
    assert not changes_context(sql)


# --- the leak check on Snowflake, against a stand-in


class StubSnowflake:
    """Just enough of a Snowflake session for the leak check: schemas, temporary tables, parameters, variables and
    secondary roles, listed as Snowflake lists them. Records what ran."""

    def __init__(self) -> None:
        self.database, self.schema, self.role, self.warehouse = "DB", "PROBES_X", "PROBER", "WH"
        self.schemas = {"INFORMATION_SCHEMA", "PUBLIC", "PROBES_X"}
        self.temporary_tables: set[tuple[str, str]] = set()
        """(schema, name)"""
        # (value, type, level): TIMEZONE as the run's connection sets it, the timeout as the account would
        self.parameters = {"TIMEZONE": ("UTC", "STRING", "SESSION"), "LOCK_TIMEOUT": ("43200", "NUMBER", "")}
        self.variables: dict[str, tuple[str | None, str]] = {}
        self.secondary_roles = '{"roles":"","value":"ALL"}'
        self.statements: list[str] = []

    def cursor(self) -> "StubCursor":
        return StubCursor(self)

    def rollback(self) -> None:
        self.statements.append("rollback")

    def autocommit(self, mode: bool) -> None:
        self.statements.append(f"autocommit {mode}")


class StubCursor:
    def __init__(self, conn: StubSnowflake) -> None:
        self.conn = conn
        self.description: list[Any] = []
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str) -> "StubCursor":
        conn = self.conn
        conn.statements.append(sql)
        text = re.sub(r"/\*.*?\*/", " ", sql).strip()
        self.result(["status"], [("ok",)])
        if m := re.fullmatch(r"create schema \w+\.(\w+)", text):
            conn.schemas.add(m[1])
            conn.schema = m[1]
        elif m := re.fullmatch(r"drop schema if exists \w+\.(\w+) cascade", text):
            conn.schemas.discard(m[1])
        elif m := re.fullmatch(r"create temporary table \w+\.(\w+)\.(\w+) \(x int\)", text, re.IGNORECASE):
            conn.temporary_tables.add((m[1].upper(), m[2].upper()))
        elif m := re.fullmatch(r'drop table if exists "DB"\."(\w+)"\."(\w+)"', text):
            conn.temporary_tables.discard((m[1], m[2]))
        elif text.startswith("show terse tables"):
            # TERSE says TABLE for a temporary table too (checked live)
            rows = [(None, name, "TABLE", conn.database, schema) for schema, name in sorted(conn.temporary_tables)]
            self.result(["created_on", "name", "kind", "database_name", "schema_name"], rows)
        elif text.startswith("show terse"):
            listed = conn.schemas if "schemas" in text else []
            rows = [(None, name, None, conn.database, None) for name in sorted(listed)]
            self.result(["created_on", "name", "kind", "database_name", "schema_name"], rows)
        elif text.startswith("show tables in database"):
            rows = [
                (None, name, conn.database, schema, "TEMPORARY", "", "", 0, 0, "PROBER", "1")
                for schema, name in sorted(conn.temporary_tables)
            ]
            columns = ["created_on", "name", "database_name", "schema_name", "kind", "comment", "cluster_by"]
            self.result([*columns, "rows", "bytes", "owner", "retention_time"], rows)
        elif text == "show parameters in session":
            rows = [(k, v, "", level, "", t) for k, (v, t, level) in conn.parameters.items()]
            self.result(["key", "value", "default", "level", "description", "type"], rows)
        elif text == "show variables":
            rows = [(1, None, None, k, v, t, None) for k, (v, t) in conn.variables.items()]
            self.result(["session_id", "created_on", "updated_on", "name", "value", "type", "comment"], rows)
        elif text == "select current_secondary_roles()":
            self.result(["CURRENT_SECONDARY_ROLES()"], [(conn.secondary_roles,)])
        elif m := re.fullmatch(r"use secondary roles (all|none)", text, re.IGNORECASE):
            conn.secondary_roles = '{"roles":"","value":"ALL"}' if m[1].lower() == "all" else '{"roles":"","value":""}'
        elif m := re.fullmatch(r"alter\s+session\s+set\s+(\w+)\s*=\s*'([^']*)'", text, re.IGNORECASE):
            conn.parameters[m[1].upper()] = (m[2], "STRING", "SESSION")
        elif m := re.fullmatch(r"alter\s+session\s+set\s+(\w+)\s*=\s*(\d+)", text, re.IGNORECASE):
            conn.parameters[m[1].upper()] = (m[2], "NUMBER", "SESSION")
        elif m := re.fullmatch(r"alter session unset (\w+)", text):
            conn.parameters[m[1]] = ("43200", "NUMBER", "")  # the stand-in's only inherited parameter
        elif m := re.fullmatch(r"set\s+(\w+)\s*=\s*(\d+)", text, re.IGNORECASE):
            conn.variables[m[1].upper()] = (m[2], "fixed")
        elif m := re.fullmatch(r"set\s+(\w+)\s*=\s*'([^']*)'", text, re.IGNORECASE):
            conn.variables[m[1].upper()] = (m[2], "text")
        elif m := re.fullmatch(r"set (\w+) = cast\(null as varchar\)", text):
            conn.variables[m[1]] = (None, "text")
        elif m := re.fullmatch(r"unset\s+(\w+)", text, re.IGNORECASE):
            conn.variables.pop(m[1].upper(), None)
        return self

    def result(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        self.description = [
            SimpleNamespace(name=c, type_code=2, precision=None, scale=None, internal_size=None) for c in columns
        ]
        self.rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def close(self) -> None:
        pass


def stub_session() -> tuple[Session, StubSnowflake]:
    conn = StubSnowflake()
    fake: Any = conn
    return Session(SNOWFLAKE, Names("DB", "PROBES_X"), fake, lambda schema: fake, {}), conn


def test_a_parameter_change_is_blamed_on_the_case_that_made_it_and_undone():
    session, conn = stub_session()
    check = LeakCheck(session)
    changed = select("changes", "ALTER /* c */ SESSION SET TIMEZONE = 'America/New_York'")
    outcomes = [run_case(case, session, check) for case in [changed, select("next"), select("after")]]
    assert outcomes[0].leaks == ("session parameter TIMEZONE changed from 'UTC' to 'America/New_York'",)
    assert outcomes[1].leaks == outcomes[2].leaks == ()
    assert conn.parameters["TIMEZONE"] == ("UTC", "STRING", "SESSION")


def test_an_inherited_parameter_is_unset_rather_than_set_to_its_value():
    session, conn = stub_session()
    check = LeakCheck(session)
    outcome = run_case(select("changes", "alter session set lock_timeout = 5"), session, check)
    assert outcome.leaks == ("session parameter LOCK_TIMEOUT changed from '43200' to '5'",)
    assert "alter session unset LOCK_TIMEOUT" in conn.statements
    assert conn.parameters["LOCK_TIMEOUT"] == ("43200", "NUMBER", "")


def test_a_temporary_table_on_snowflake_is_found_by_show_tables_and_dropped():
    # TERSE lists it as TABLE: only the full SHOW TABLES says TEMPORARY
    session, conn = stub_session()
    check = LeakCheck(session)
    outcome = run_case(select("leaves", "create temporary table {database}.PROBES_X.home_tt (x int)"), session, check)
    assert outcome.leaks == ("temporary table {database}.{run_schema}.HOME_TT left behind",)
    assert 'drop table if exists "DB"."PROBES_X"."HOME_TT"' in conn.statements
    assert conn.temporary_tables == set()


def test_a_null_variable_unset_is_set_back_to_a_null_of_its_type():
    session, conn = stub_session()
    conn.variables["N"] = (None, "text")
    check = LeakCheck(session)
    outcome = run_case(select("unsets", "unset n"), session, check)
    assert outcome.leaks == ("session variable N unset",)
    assert conn.variables == {"N": (None, "text")}
    assert "set N = cast(null as varchar)" in conn.statements


def test_a_variable_whose_type_changed_is_a_leak():
    session, conn = stub_session()
    conn.variables["V"] = ("1", "fixed")
    check = LeakCheck(session)
    outcome = run_case(select("retypes", "set v = '1'"), session, check)
    assert outcome.leaks == ("session variable V changed from '1' (fixed) to '1' (text)",)
    assert conn.variables == {"V": ("1", "fixed")}


def python_probe(case_id: str, sql: str) -> Case:
    return Case(id=case_id, description="d", run=lambda conn, ctx: conn.cursor().execute(sql) and None)


def test_secondary_roles_a_python_probe_changed_are_a_leak_and_set_back():
    session, conn = stub_session()
    check = LeakCheck(session)
    outcome = run_case(python_probe("secondary", "use secondary roles none"), session, check)
    assert outcome.leaks == (
        'session secondary roles changed from {"roles":"","value":"ALL"} to {"roles":"","value":""}',
    )
    assert conn.secondary_roles == '{"roles":"","value":"ALL"}'


def test_a_session_variable_is_reported_and_unset():
    session, conn = stub_session()
    check = LeakCheck(session)
    outcomes = [run_case(case, session, check) for case in [select("sets", "SET reviewer_variable=42"), select("next")]]
    assert outcomes[0].leaks == ("session variable REVIEWER_VARIABLE set to '42'",)
    assert outcomes[1].leaks == ()
    assert conn.variables == {}
    assert "unset REVIEWER_VARIABLE" in conn.statements


def round_trips(case: Case) -> list[str]:
    """What the runner sends Snowflake for a case, beyond the case's own statements."""
    session, conn = stub_session()
    check = LeakCheck(session)
    conn.statements.clear()
    run_case(case, session, check)
    own = {session.run_names.case(case.id).render(s) for s in (*case.setup, *case.statements, *case.teardown)}
    return [re.sub(r"PROBES_X\w*", "X", s).split(" in ")[0] for s in conn.statements if s not in own]


def test_a_case_that_cant_change_shared_state_costs_its_schema_only():
    assert round_trips(select("a", "select 1")) == ["create schema DB.X", "drop schema if exists DB.X cascade"]


def test_ddl_on_schemas_adds_a_snapshot_of_objects_only():
    assert round_trips(select("a", "drop schema if exists {database}.{schema}_S")) == [
        "create schema DB.X",
        "drop schema if exists DB.X cascade",
        "show terse schemas",
        "show terse databases like 'X%'",
        "show tables",
    ]


def test_a_case_that_could_change_the_session_resets_it_and_lists_everything():
    assert round_trips(select("a", "set x = 1")) == [
        "create schema DB.X",
        "rollback",
        "autocommit True",
        "drop schema if exists DB.X cascade",
        "show terse schemas",
        "show terse databases like 'X%'",
        "show tables",
        "show parameters",
        "show variables",
        "select current_secondary_roles()",
        "unset X",
        "show terse schemas",
        "show terse databases like 'X%'",
        "show tables",
        "show parameters",
        "show variables",
        "select current_secondary_roles()",
    ]


def test_a_teardown_that_could_change_the_session_is_followed_by_another_reset():
    case = Case(id="a", description="d", sql="select 1", teardown=["alter session set autocommit = false"])
    assert round_trips(case)[:5] == ["create schema DB.X", "rollback", "autocommit True", "rollback", "autocommit True"]


def test_snowflake_leaks_and_how_they_are_undone():
    session, _ = stub_session()
    check = LeakCheck.__new__(LeakCheck)
    check.session = session
    before = State(
        schemas=frozenset({"PUBLIC", "PROBES_X"}),
        context={"role": "PROBER", "warehouse": "WH", "secondary roles": '{"roles":"","value":""}'},
        parameters={
            "TIMEZONE": Parameter("UTC", "STRING", "SESSION"),
            "STATEMENT_TIMEOUT_IN_SECONDS": Parameter("60", "NUMBER", "SESSION"),
            "LOCK_TIMEOUT": Parameter("43200", "NUMBER", ""),
            "ODD": Parameter("x", "ENUM?", "SESSION"),
        },
        variables={
            "KEPT": ("1", "fixed"),
            "GONE": ("a'b", "text"),
            "NULL_GONE": (None, "fixed"),
            "RETYPED": ("1", "fixed"),
            "NULLED": ("5", "fixed"),
            "ODD": ("2026-10-29", "date"),
        },
    )
    after = State(
        schemas=frozenset({"PROBES_X", "PROBES_X_C1_S2", "SOMEONE_ELSES"}),
        databases=frozenset({"PROBES_X_C1_DB"}),
        temporary_tables=frozenset({("DB", "PROBES_X", "MINE"), ("DB", "PUBLIC", "THEIRS")}),
        context={"role": "OTHER", "warehouse": "WH", "secondary roles": '{"roles":"R1","value":"R1"}'},
        parameters={
            "TIMEZONE": Parameter("America/New_York", "STRING", "SESSION"),
            "STATEMENT_TIMEOUT_IN_SECONDS": Parameter("5", "NUMBER", "SESSION"),
            "LOCK_TIMEOUT": Parameter("1", "NUMBER", "SESSION"),
            "ODD": Parameter("y", "ENUM?", "SESSION"),
        },
        variables={
            "KEPT": ("2", "fixed"),
            "new_one": ("x", "text"),
            "NULLED": (None, "fixed"),
            "RETYPED": ("1", "text"),
            "ODD": ("2026-10-30", "date"),
        },
    )
    leaks = {leak.description: leak.undo for leak in check.changes(before, after)}
    assert leaks == {
        "schema DB.PROBES_X_C1_S2 left behind": 'drop schema if exists DB."PROBES_X_C1_S2" cascade',
        # the database is shared: only what's named after the run is dropped
        "schema DB.SOMEONE_ELSES left behind, not named after the run": None,
        "schema DB.PUBLIC dropped": None,
        "database PROBES_X_C1_DB left behind": 'drop database if exists "PROBES_X_C1_DB" cascade',
        "temporary table DB.PROBES_X.MINE left behind": 'drop table if exists "DB"."PROBES_X"."MINE"',
        # DROP TABLE drops a permanent table of the same name if the temporary one is gone
        "temporary table DB.PUBLIC.THEIRS left behind": None,
        "session role changed from PROBER to OTHER": 'use role "PROBER"',
        'session secondary roles changed from {"roles":"","value":""} to {"roles":"R1","value":"R1"}': (
            "use secondary roles none"
        ),
        "session parameter STATEMENT_TIMEOUT_IN_SECONDS changed from '60' to '5'": (
            "alter session set STATEMENT_TIMEOUT_IN_SECONDS = 60"
        ),
        "session parameter TIMEZONE changed from 'UTC' to 'America/New_York'": "alter session set TIMEZONE = 'UTC'",
        # inherited before: unset, so it's inherited again
        "session parameter LOCK_TIMEOUT changed from '43200' to '1'": "alter session unset LOCK_TIMEOUT",
        # a type the runner can't write a literal of: reported, not undone
        "session parameter ODD changed from 'x' to 'y'": None,
        "session variable new_one set to 'x'": 'unset "new_one"',
        "session variable GONE unset": "set GONE = 'a\\'b'",
        "session variable NULL_GONE unset": "set NULL_GONE = cast(null as number)",
        "session variable KEPT changed from '1' to '2'": "set KEPT = 1",
        "session variable RETYPED changed from '1' (fixed) to '1' (text)": "set RETYPED = 1",
        "session variable NULLED changed from '5' to None": "set NULLED = 5",
        "session variable ODD changed from '2026-10-29' to '2026-10-30'": None,
    }


# --- comparing and reporting leaks


RESULT = Outcome(steps=(Step("select 1", ResultSet((Column("X", 0),), ((1,),))),))
LEAKED = ("schema {database}.{schema}_S2 left behind",)


def test_a_reference_leak_is_an_error_only_when_the_reference_is_snowflake():
    case = select("a")
    leaked, clean = Outcome(steps=RESULT.steps, leaks=LEAKED), Outcome(steps=RESULT.steps, leaks=())
    verdict = classify(case, leaked, leaked, FAKESNOW, with_expected=True)
    assert verdict.status is Status.ERROR
    assert verdict.differences == (f"reference: leaked {LEAKED[0]}: undo it in the case's teardown",)
    # fakesnow as the reference: leaks are compared like the rest of the outcome
    assert classify(case, leaked, leaked, FAKESNOW_SERVER, with_expected=False).status is Status.MATCH
    assert classify(case, leaked, clean, FAKESNOW_SERVER, with_expected=False).status is Status.UNEXPECTED


def test_a_candidate_only_leak_is_a_difference_that_can_be_pinned():
    reference = Outcome(steps=RESULT.steps, leaks=())
    candidate = Outcome(steps=RESULT.steps, leaks=LEAKED)
    verdict = classify(select("a"), reference, candidate, FAKESNOW, with_expected=True)
    assert verdict.status is Status.UNEXPECTED
    assert verdict.differences == (f"leaked {LEAKED[0]}",)
    assert verdict.fingerprint
    pinned = Case(
        id="a",
        description="d",
        sql="select 1",
        expected=Expected(["#1"], "fakesnow can't drop it"),
        expected_outcomes={FAKESNOW: verdict.fingerprint},
    )
    assert classify(pinned, reference, candidate, FAKESNOW, with_expected=True).status is Status.EXPECTED


@pytest.mark.parametrize("with_expected", [True, False], ids=["snowflake", "fakesnow"])
def test_a_candidate_leak_against_an_unchecked_reference_is_an_error_not_a_difference(with_expected: bool):
    unchecked = Outcome(steps=RESULT.steps)  # recorded before the leak check
    candidate = Outcome(steps=RESULT.steps, leaks=LEAKED)
    fingerprint = comparison_fingerprint(select("a"), unchecked, candidate)
    # even if someone pinned it
    pinned = Case(
        id="a",
        description="d",
        sql="select 1",
        expected=Expected(["#1"], "fakesnow can't drop it"),
        expected_outcomes={FAKESNOW: fingerprint},
    )
    verdict = classify(pinned, unchecked, candidate, FAKESNOW, with_expected=with_expected)
    assert verdict.status is Status.ERROR
    assert verdict.differences == (
        f"reference: leaks not checked (recorded before the leak check), rerun it: {FAKESNOW} leaked {LEAKED[0]}",
    )
    assert verdict.fingerprint is None


def test_an_unchecked_reference_still_compares_the_rest():
    unchecked = Outcome(steps=RESULT.steps)
    assert classify(select("a"), unchecked, Outcome(steps=RESULT.steps, leaks=()), FAKESNOW, with_expected=True) == (
        classify(select("a"), RESULT, RESULT, FAKESNOW, with_expected=True)
    )


def test_no_leaks_keeps_the_fingerprint_from_before_the_leak_check():
    case = select("a")
    other = Outcome(steps=(Step("select 1", ResultSet((Column("X", 0),), ((2,),))),))
    before_check = comparison_fingerprint(case, RESULT, other)
    assert comparison_fingerprint(case, RESULT, Outcome(steps=other.steps, leaks=())) == before_check
    assert comparison_fingerprint(case, RESULT, Outcome(steps=other.steps, leaks=LEAKED)) != before_check


def test_leaks_round_trip_and_old_reports_have_none():
    assert decode(encode(Outcome(leaks=LEAKED))).leaks == LEAKED
    assert decode(encode(Outcome(leaks=()))).leaks == ()
    unchecked = encode(Outcome())
    assert "leaks" not in unchecked["$outcome"]
    assert decode(unchecked).leaks is None


def test_reports_list_leaks(tmp_path: Path):
    cases = load_cases([case_dir(tmp_path, "leak", LEAKS)])[:1]
    names = Names.new("PROBES")
    report = compare_runs(cases, run_target(FAKESNOW, cases, names), [run_target(FAKESNOW_SERVER, cases, names)], names)
    assert "1 leaked" in summary(report)
    assert f"{FAKESNOW_SERVER:<16} leak.schema: 'schema {{database}}.{{schema}}_L left behind'" in summary(report)
    assert "## Leaks" in markdown(report)
    assert report.verdicts["leak.schema"][FAKESNOW_SERVER].status is Status.MATCH


def test_reports_say_what_the_leak_check_covered_and_that_fakesnow_variables_arent(tmp_path: Path):
    cases = [select("a")]
    names = Names.new("PROBES")
    report = compare_runs(cases, run_target(FAKESNOW, cases, names), [run_target(FAKESNOW_SERVER, cases, names)], names)
    scope = "schemas, databases, temporary tables; variables unchecked (fakesnow doesn't list them)"
    assert leak_scope(report) == f"Leak check: {FAKESNOW}: {scope}; {FAKESNOW_SERVER}: {scope}."
    assert leak_scope(report) in summary(report)
    assert leak_scope(report) in markdown(report)
    write_json(report, tmp_path / "report.json")
    assert load_run(tmp_path / "report.json", FAKESNOW).leak_check == scope


def test_reports_say_a_recorded_reference_wasnt_checked():
    cases = [select("a")]
    names = Names.new("PROBES")
    reference = run_target(FAKESNOW, cases, names)
    reference.outcomes["a"] = Outcome(steps=reference.outcomes["a"].steps)
    reference.recorded, reference.leak_check = True, None
    report = compare_runs(cases, reference, [run_target(FAKESNOW_SERVER, cases, names)], names)
    assert leak_scope(report).startswith(f"Leak check: {FAKESNOW} (recorded): not checked (recorded before the leak")
    assert leak_scope(report).endswith(
        "A leak against an unchecked reference is an error until the reference is rerun."
    )


# --- shuffled order


def test_shuffle_is_deterministic_for_a_seed_and_differs_between_seeds():
    cases = [select(f"c{i}") for i in range(20)]
    assert [c.id for c in shuffled(cases, 7)] == [c.id for c in shuffled(cases, 7)]
    assert [c.id for c in shuffled(cases, 7)] != [c.id for c in shuffled(cases, 8)]
    assert [c.id for c in shuffled(cases, 7)] != [c.id for c in cases]


def test_a_shuffled_run_uses_the_same_order_on_every_target_and_reports_in_file_order(tmp_path: Path):
    loaded = load_cases([case_dir(tmp_path, "iso", CASES)])
    order = shuffled(loaded, 3)
    report = run(loaded, order, FAKESNOW, [FAKESNOW_SERVER], None, verbose=False, seed=3)
    assert list(report.reference.outcomes) == list(report.candidates[0].outcomes) == [c.id for c in order]
    assert [c.id for c in report.cases] == [c.id for c in loaded]


def test_every_report_has_the_seed_and_the_order(tmp_path: Path):
    loaded = load_cases([case_dir(tmp_path, "iso", CASES)])
    order = shuffled(loaded, 3)
    report = run(loaded, order, FAKESNOW, [FAKESNOW_SERVER], None, verbose=False, seed=3)
    assert "--shuffle 3" in summary(report)
    assert "--shuffle 3" in markdown(report)
    assert to_json(report)["order"] == {"shuffle": 3, "cases": [c.id for c in order]}


def test_shuffle_random_picks_a_seed_and_reports_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = case_dir(tmp_path, "iso", CASES)
    js = tmp_path / "report.json"
    assert (
        main(
            [str(path), "--reference", FAKESNOW, "--target", FAKESNOW_SERVER, "--json", str(js), "--shuffle", "random"]
        )
        == 0
    )
    seed = json.loads(js.read_text())["order"]["shuffle"]
    assert f"--shuffle {seed}" in capsys.readouterr().out
    assert json.loads(js.read_text())["order"]["cases"] == [c.id for c in shuffled(load_cases([path]), seed)]


def test_order_runs_exactly_the_cases_given(tmp_path: Path):
    path = str(case_dir(tmp_path, "iso", CASES))
    js = tmp_path / "report.json"
    assert (
        main(
            [
                path,
                "--reference",
                FAKESNOW,
                "--target",
                FAKESNOW_SERVER,
                "--order",
                "iso.read,iso.create",
                "--json",
                str(js),
            ]
        )
        == 0
    )
    assert json.loads(js.read_text())["order"]["cases"] == ["iso.read", "iso.create"]


@pytest.mark.parametrize("extra", [["--order", "iso.nope"], ["--order", "iso.read", "--shuffle", "1"]])
def test_order_rejects_unknown_cases_and_shuffle(tmp_path: Path, extra: list[str]):
    path = str(case_dir(tmp_path, "iso", CASES))
    assert main([path, "--reference", FAKESNOW, "--target", FAKESNOW_SERVER, *extra]) == 2


# --- finding polluters


def test_ddmin_finds_a_smallest_set_in_order():
    items = list(range(40))
    assert ddmin(items, lambda s: 17 in s) == [17]
    assert ddmin(items, lambda s: 3 in s and 31 in s) == [3, 31]
    assert ddmin(items, lambda s: len({5, 6, 7} & set(s)) == 3) == [5, 6, 7]


def test_find_polluters_finds_a_planted_pair(tmp_path: Path):
    cases = load_cases([case_dir(tmp_path, "p", POLLUTERS)])
    ids = [c.id for c in cases]
    search = find_polluters(cases[ids.index("p.victim")], cases[: ids.index("p.victim")], FAKESNOW, "PROBES")
    # each variable alone fails the same way as neither: only both change the outcome
    assert [c.id for c in search.polluters] == ["p.x", "p.y"]
    assert search.changes and search.after_polluters


def test_find_polluters_finds_what_the_leak_check_undoes_only_with_leaks_kept(tmp_path: Path):
    cases = load_cases([case_dir(tmp_path, "p", POLLUTERS)])
    ids = [c.id for c in cases]
    lister, before = cases[ids.index("p.lister")], cases[: ids.index("p.lister")]
    assert not find_polluters(lister, before, FAKESNOW, "PROBES").polluters
    kept = find_polluters(lister, before, FAKESNOW, "PROBES", cleanup=False)
    assert [c.id for c in kept.polluters] == ["p.schema"]


def test_server_trials_dont_see_each_other_so_an_innocent_case_isnt_blamed():
    victim = select("victim", "select count(*) from information_schema.databases")
    innocent, creator = select("innocent"), select("creator", "create database {schema}_DB")
    search = find_polluters(victim, [innocent, creator], FAKESNOW_SERVER, "PROBES")
    assert [c.id for c in search.polluters] == ["creator"]


def test_a_case_schema_name_doesnt_make_the_cases_before_it_polluters():
    victim = select("victim", "select length(current_schema())")
    before = [select(f"before.{i}") for i in range(10)]
    assert not find_polluters(victim, before, FAKESNOW, "PROBES").changes


def test_find_polluter_cli_prints_the_polluters_and_how_to_reproduce(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    path = str(case_dir(tmp_path, "p", POLLUTERS))
    assert main([path, "--find-polluter", "p.victim"]) == 0
    out = capsys.readouterr().out
    assert "  p.x\n  p.y\n" in out
    assert f"reproduce: python -m probes {path} --find-polluter p.victim --order p.x,p.y,p.victim" in out


def test_find_polluter_cli_reproduces_with_the_same_options_quoted(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = str(case_dir(tmp_path, "with space", POLLUTERS))
    assert main([path, "--find-polluter", "p.victim", "--strict-metadata", "--keep-leaks"]) == 0
    repro = capsys.readouterr().out.split("reproduce: ")[1].strip()
    assert repro == (
        f"python -m probes '{path}' --find-polluter p.victim --order p.x,p.y,p.victim --target {FAKESNOW} "
        "--strict-metadata --keep-leaks"
    )


@pytest.mark.parametrize(
    ("extra", "status"),
    [(["--find-polluter", "p.a"], 1), (["--find-polluter", "p.victim", "--target", SNOWFLAKE], 2)],
    ids=["nothing changes it", "snowflake"],
)
def test_find_polluter_cli_exit_status(tmp_path: Path, extra: list[str], status: int):
    assert main([str(case_dir(tmp_path, "p", POLLUTERS)), *extra]) == status


def test_find_polluters_refuses_snowflake():
    with pytest.raises(TargetError, match="not snowflake"):
        find_polluters(select("v"), [], SNOWFLAKE, "PROBES")
