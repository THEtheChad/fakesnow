"""Isolation: each case runs in a schema of its own. No Snowflake."""

from pathlib import Path

import pytest

from probes.loader import load_cases
from probes.outcome import ResultSet, scrubber
from probes.runner import run_target
from probes.targets import FAKESNOW, FAKESNOW_SERVER, Names

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


@pytest.mark.parametrize("target", [FAKESNOW, FAKESNOW_SERVER])
def test_each_case_runs_in_its_own_schema(tmp_path: Path, target: str):
    cases = load_cases([case_dir(tmp_path, "iso", CASES)])
    run = run_target(target, cases, Names.new("PROBES"))
    created = run.outcomes["iso.create"]
    assert created.steps[1].result
    assert created.steps[1].result.rows == (("{schema}", "{schema}_S2", "{schema:lower}"),)
    read = run.outcomes["iso.read"]
    assert read.steps[0].error
    assert read.steps[0].error.sqlstate == "42S02"
    session = run.outcomes["iso.session"].value
    assert isinstance(session, ResultSet)
    assert session.rows == (("{schema}",),)
