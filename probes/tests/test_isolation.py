"""Isolation: each case's own schema, the leak check, shuffled order, and finding polluters. No Snowflake."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from probes.cli import main, run
from probes.compare import Status, classify, comparison_fingerprint
from probes.isolation import LeakCheck, State, checks_parameters
from probes.loader import load_cases
from probes.model import Case, Expected
from probes.outcome import Column, Outcome, ResultSet, Step, decode, encode, scrubber
from probes.polluter import ddmin, find_polluters
from probes.report import markdown, summary, to_json
from probes.runner import compare_runs, run_case, run_target, shuffled
from probes.targets import FAKESNOW, FAKESNOW_SERVER, SNOWFLAKE, Names, TargetError, open_target

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


# --- each case's own schema


def test_case_names_are_the_runs_with_a_suffix_and_scrub_to_schema():
    run = Names("db", "probes_x")
    case = run.case(3)
    assert (case.database, case.schema, case.run, run.run) == ("DB", "PROBES_X_C3", "PROBES_X", "PROBES_X")
    assert case.render("{database}.{schema}_S2") == "DB.PROBES_X_C3_S2"
    scrub = scrubber(case.placeholders)
    assert scrub("PROBES_X_C3 PROBES_X_C3_S2 probes_x_c3 DB") == "{schema} {schema}_S2 {schema:lower} {database}"
    # another case's schema, or the run's own, is never this case's {schema}
    assert scrub("PROBES_X_C33 PROBES_X") == "{run_schema}_C33 {run_schema}"


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
    # every fakesnow server in a process shares one instance
    case = Case(id="count", description="d", sql="select count(*) from information_schema.schemata")
    first = run_target(FAKESNOW_SERVER, [case], Names.new("PROBES")).outcomes["count"]
    assert run_target(FAKESNOW_SERVER, [case], Names.new("PROBES")).outcomes["count"] == first


# --- the leak check


def test_leaks_are_reported_against_the_case_and_undone(tmp_path: Path):
    cases = load_cases([case_dir(tmp_path, "leak", LEAKS)])
    with open_target(FAKESNOW, Names.new("PROBES")) as session:
        check = LeakCheck(session)
        outcomes = [run_case(case, session, n, check) for n, case in enumerate(cases, start=1)]
        cur = session.conn.cursor()
        cur.execute(f"select count(*) from information_schema.schemata where schema_name = '{session.names.run}_C1_L'")
        assert cur.fetchall() == [(0,)]
    assert outcomes[0].leaks == ("schema {database}.{schema}_L left behind",)
    # fakesnow keeps a temporary table after its schema is dropped: the runner drops it, as Snowflake would
    assert outcomes[1].leaks == ()
    assert outcomes[2].leaks == ()
    assert outcomes[2].steps[0].error is None


def test_keep_leaks_reports_each_once_and_leaves_it(tmp_path: Path):
    cases = load_cases([case_dir(tmp_path, "leak", LEAKS)])
    with open_target(FAKESNOW, Names.new("PROBES")) as session:
        check = LeakCheck(session, cleanup=False)
        first = run_case(cases[0], session, 1, check)
        second = run_case(Case(id="quiet", description="does nothing", sql="select 1"), session, 2, check)
        cur = session.conn.cursor()
        cur.execute(f"select count(*) from information_schema.schemata where schema_name = '{session.names.run}_C1_L'")
        assert cur.fetchall() == [(1,)]
    assert first.leaks and not second.leaks


def fake_snowflake_check(names: Names) -> LeakCheck:
    check = LeakCheck.__new__(LeakCheck)
    check.session = SimpleNamespace(run_names=names, is_snowflake=True)  # pyright: ignore[reportAttributeAccessIssue]
    return check


def test_snowflake_leaks_and_how_they_are_undone():
    names = Names("DB", "PROBES_X")
    check = fake_snowflake_check(names)
    before = State(
        frozenset({"PUBLIC", "PROBES_X"}),
        frozenset(),
        {"role": "PROBER", "warehouse": "WH"},
        {"TIMEZONE": ("UTC", "STRING"), "STATEMENT_TIMEOUT_IN_SECONDS": ("60", "NUMBER")},
    )
    after = State(
        frozenset({"PROBES_X", "PROBES_X_C1_S2", "SOMEONE_ELSES"}),
        frozenset({"PROBES_X_C1_DB"}),
        {"role": "OTHER", "warehouse": "WH"},
        {"TIMEZONE": ("America/New_York", "STRING"), "STATEMENT_TIMEOUT_IN_SECONDS": ("5", "NUMBER")},
    )
    leaks = {leak.description: leak.undo for leak in check.changes(before, after)}
    assert leaks == {
        "schema DB.PROBES_X_C1_S2 left behind": 'drop schema if exists DB."PROBES_X_C1_S2" cascade',
        # the database is shared: only what's named after the run is dropped
        "schema DB.SOMEONE_ELSES left behind, not named after the run": None,
        "schema DB.PUBLIC dropped": None,
        "database PROBES_X_C1_DB left behind": 'drop database if exists "PROBES_X_C1_DB" cascade',
        "session role changed from PROBER to OTHER": 'use role "PROBER"',
        "session parameter STATEMENT_TIMEOUT_IN_SECONDS changed from '60' to '5'": (
            "alter session set STATEMENT_TIMEOUT_IN_SECONDS = 60"
        ),
        "session parameter TIMEZONE changed from 'UTC' to 'America/New_York'": "alter session set TIMEZONE = 'UTC'",
    }


def test_session_parameters_are_listed_only_on_snowflake_after_cases_that_could_change_them():
    snowflake: Any = SimpleNamespace(is_snowflake=True)
    fakesnow: Any = SimpleNamespace(is_snowflake=False)
    plain = Case(id="a", description="d", sql="select 'alter session' as text_is_not_checked_differently")
    assert checks_parameters(snowflake, Case(id="a", description="d", sql="ALTER  SESSION set timezone = 'UTC'"))
    assert checks_parameters(snowflake, Case(id="a", description="d", run=lambda conn, ctx: None))
    assert not checks_parameters(snowflake, Case(id="a", description="d", sql="select 1"))
    assert checks_parameters(snowflake, plain)  # a false positive costs one statement, never a missed leak
    assert not checks_parameters(fakesnow, Case(id="a", description="d", run=lambda conn, ctx: None))


RESULT = Outcome(steps=(Step("select 1", ResultSet((Column("X", 0),), ((1,),))),))
LEAKED = ("schema {database}.{schema}_S2 left behind",)


def test_a_reference_leak_is_an_error_only_when_the_reference_is_snowflake():
    case = Case(id="a", description="d", sql="select 1")
    leaked, clean = Outcome(steps=RESULT.steps, leaks=LEAKED), Outcome(steps=RESULT.steps, leaks=())
    verdict = classify(case, leaked, leaked, FAKESNOW, with_expected=True)
    assert verdict.status is Status.ERROR
    assert verdict.differences == (f"reference: leaked {LEAKED[0]}: undo it in the case's teardown",)
    # fakesnow as the reference: leaks are compared like the rest of the outcome
    assert classify(case, leaked, leaked, FAKESNOW_SERVER, with_expected=False).status is Status.MATCH
    assert classify(case, leaked, clean, FAKESNOW_SERVER, with_expected=False).status is Status.UNEXPECTED


@pytest.mark.parametrize("reference_leaks", [(), None], ids=["checked", "recorded before the leak check"])
def test_a_candidate_only_leak_is_a_difference(reference_leaks: tuple[str, ...] | None):
    case = Case(id="a", description="d", sql="select 1")
    reference = Outcome(steps=RESULT.steps, leaks=reference_leaks)
    candidate = Outcome(steps=RESULT.steps, leaks=LEAKED)
    verdict = classify(case, reference, candidate, FAKESNOW, with_expected=True)
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


def test_no_leaks_keeps_the_fingerprint_from_before_the_leak_check():
    case = Case(id="a", description="d", sql="select 1")
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


# --- shuffled order


def test_shuffle_is_deterministic_and_the_seed_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    cases = [Case(id=f"c{i}", description="d", sql="select 1") for i in range(20)]
    assert [c.id for c in shuffled(cases, 7)] == [c.id for c in shuffled(cases, 7)]
    assert [c.id for c in shuffled(cases, 7)] != [c.id for c in shuffled(cases, 8)]
    assert [c.id for c in shuffled(cases, 7)] != [c.id for c in cases]

    loaded = load_cases([case_dir(tmp_path, "iso", CASES)])
    order = shuffled(loaded, 3)
    report = run(loaded, order, FAKESNOW, [FAKESNOW_SERVER], None, verbose=False, seed=3)
    # the same order on every target, reported in file order
    assert list(report.reference.outcomes) == list(report.candidates[0].outcomes) == [c.id for c in order]
    assert [c.id for c in report.cases] == [c.id for c in loaded]
    assert "--shuffle 3" in summary(report)
    assert "--shuffle 3" in markdown(report)
    assert to_json(report)["order"] == {"shuffle": 3, "cases": [c.id for c in order]}

    js = tmp_path / "report.json"
    args = [str(tmp_path / "iso"), "--reference", FAKESNOW, "--target", FAKESNOW_SERVER, "--json", str(js)]
    assert main([*args, "--shuffle", "random"]) == 0
    seed = json.loads(js.read_text())["order"]["shuffle"]
    assert f"--shuffle {seed}" in capsys.readouterr().out
    assert json.loads(js.read_text())["order"]["cases"] == [c.id for c in shuffled(loaded, seed)]


def test_order_runs_exactly_the_cases_given(tmp_path: Path):
    path = str(case_dir(tmp_path, "iso", CASES))
    base = [path, "--reference", FAKESNOW, "--target", FAKESNOW_SERVER]
    assert main([*base, "--order", "iso.read,iso.create"]) == 0
    assert main([*base, "--order", "iso.nope"]) == 2
    assert main([*base, "--order", "iso.read", "--shuffle", "1"]) == 2


# --- finding polluters


def test_ddmin_finds_a_smallest_set_in_order():
    items = list(range(40))
    assert ddmin(items, lambda s: 17 in s) == [17]
    assert ddmin(items, lambda s: 3 in s and 31 in s) == [3, 31]
    assert ddmin(items, lambda s: len({5, 6, 7} & set(s)) == 3) == [5, 6, 7]


def test_find_polluters_finds_a_planted_pair(tmp_path: Path):
    cases = load_cases([case_dir(tmp_path, "p", POLLUTERS)])
    ids = [c.id for c in cases]
    victim = cases[ids.index("p.victim")]
    search = find_polluters(victim, cases[: ids.index("p.victim")], FAKESNOW, "PROBES")
    # each variable alone fails the same way as neither: only both change the outcome
    assert [c.id for c in search.polluters] == ["p.x", "p.y"]
    assert search.changes and search.after_polluters

    lister = cases[ids.index("p.lister")]
    before = cases[: ids.index("p.lister")]
    # the leak check undoes the leaked schema, so nothing before the lister changes it ...
    assert not find_polluters(lister, before, FAKESNOW, "PROBES").polluters
    # ... unless leaks are kept
    kept = find_polluters(lister, before, FAKESNOW, "PROBES", cleanup=False)
    assert [c.id for c in kept.polluters] == ["p.schema"]


def test_find_polluter_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    path = str(case_dir(tmp_path, "p", POLLUTERS))
    assert main([path, "--find-polluter", "p.victim"]) == 0
    out = capsys.readouterr().out
    assert "  p.x\n  p.y\n" in out
    assert f"reproduce: python -m probes {path} --find-polluter p.victim --order p.x,p.y,p.victim" in out
    assert main([path, "--find-polluter", "p.a"]) == 1
    assert main([path, "--find-polluter", "p.victim", "--target", SNOWFLAKE]) == 2
    with pytest.raises(TargetError, match="not snowflake"):
        find_polluters(Case(id="v", description="d", sql="select 1"), [], SNOWFLAKE, "PROBES")
