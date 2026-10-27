"""End to end: run cases against fakesnow in-process and in server mode, without Snowflake."""

import json
from pathlib import Path

import pytest

from probes.cli import main
from probes.compare import Status
from probes.loader import load_cases
from probes.report import load_run, markdown, summary, to_json
from probes.runner import compare_runs, run_target
from probes.targets import FAKESNOW, FAKESNOW_SERVER, SNOWFLAKE, Names, SnowflakeConfig, TargetError, open_target

CASES = """
from snowflake.connector import SnowflakeConnection

from probes import Case, Expected, ProbeContext


def two_sessions(conn: SnowflakeConnection, ctx: ProbeContext) -> dict:
    conn.cursor().execute("create or replace temporary table run_temp (x int)")
    other = ctx.connect().cursor()
    mine = ctx.query(conn.cursor(), "select x from run_temp")
    return {"mine": mine, "other": ctx.query(other, "select x from run_temp")}


def which_target(conn: SnowflakeConnection, ctx: ProbeContext) -> str:
    return ctx.target


CASES = [
    Case(id="run.select", description="a select", sql="select 1 as x, 'a' as y"),
    Case(
        id="run.table",
        description="setup, then statements",
        setup=["create or replace table run_t (x int)"],
        sql=["insert into run_t values (1)", "select x from {database}.{schema}.run_t"],
    ),
    Case(id="run.error", description="an error", sql="select * from run_no_such_table"),
    Case(id="run.sessions", description="a Python probe with two sessions", run=two_sessions),
    Case(id="run.expected", description="marked, but matches", sql="select 2", expected=Expected(["#1"], "known")),
    Case(id="run.schema", description="moves the session", sql="create schema {database}.{schema}_S2"),
    Case(id="run.after", description="back in the run's schema", sql="select current_schema()"),
]
"""

DIFFERS = """
from probes import Case

CASES = [Case(id="run.target", description="differs by target", run=lambda conn, ctx: ctx.target)]
"""


@pytest.fixture
def case_dir(tmp_path: Path) -> Path:
    path = tmp_path / "cases"
    path.mkdir()
    (path / "run.py").write_text(CASES)
    return path


def test_fakesnow_and_server_agree(case_dir: Path):
    cases = load_cases([case_dir])
    names = Names.new("PROBES")
    reference = run_target(FAKESNOW, cases, names)
    server = run_target(FAKESNOW_SERVER, cases, names)
    report = compare_runs(cases, reference, [server], names)
    statuses = {case_id: v[FAKESNOW_SERVER].status for case_id, v in report.verdicts.items()}
    # expected differences only apply when the reference is Snowflake
    assert statuses == dict.fromkeys(statuses, Status.MATCH)
    assert not report.fails

    sessions = reference.outcomes["run.sessions"].value
    assert sessions["mine"].rows == ()
    assert sessions["other"].errno == 2003
    assert reference.outcomes["run.after"].steps[0].result.rows == (("{schema}",),)  # pyright: ignore[reportOptionalMemberAccess]
    assert reference.outcomes["run.error"].steps[0].error.sqlstate == "42S02"  # pyright: ignore[reportOptionalMemberAccess]


def test_cli_exit_code_and_reports(case_dir: Path, tmp_path: Path):
    md, js = tmp_path / "report.md", tmp_path / "report.json"
    args = [
        str(case_dir),
        "--reference",
        FAKESNOW,
        "--target",
        FAKESNOW_SERVER,
        "--markdown",
        str(md),
        "--json",
        str(js),
    ]
    assert main(args) == 0
    assert "| fakesnow-server | 7 | 0 | 0 | 0 | 0 |" in md.read_text()
    assert json.loads(js.read_text())["reference"] == FAKESNOW

    (case_dir / "differs.py").write_text(DIFFERS)
    assert main([str(case_dir), "--reference", FAKESNOW, "--target", FAKESNOW_SERVER, "-k", "run.target"]) == 1


def test_recorded_snowflake_baseline(case_dir: Path, tmp_path: Path):
    """A Snowflake run's JSON stands in for Snowflake. Here fakesnow's outcomes, relabelled, play its part."""
    cases = load_cases([case_dir])
    names = Names.new("PROBES")
    recorded = run_target(FAKESNOW, cases, names)
    recorded.target = SNOWFLAKE
    baseline = tmp_path / "snowflake.json"
    baseline.write_text(json.dumps(to_json(compare_runs(cases, recorded, [], names))))

    reference = load_run(baseline)
    candidate = run_target(FAKESNOW, cases, Names.new("PROBES"))
    report = compare_runs(cases, reference, [candidate], names)
    statuses = {case_id: v[FAKESNOW].status for case_id, v in report.verdicts.items()}
    assert statuses.pop("run.expected") is Status.NOW_MATCHES
    assert set(statuses.values()) == {Status.MATCH}
    assert "Expected differences that now match" in summary(report)
    assert "expected difference now matches" in markdown(report)

    (case_dir / "run.py").write_text(CASES.replace("select 1 as x", "select 3 as x"))
    changed = load_cases([case_dir])
    report = compare_runs(changed, reference, [run_target(FAKESNOW, changed, Names.new("PROBES"))], names)
    assert report.verdicts["run.select"][FAKESNOW].status is Status.ERROR
    assert main([str(case_dir), "--baseline", str(baseline), "--target", FAKESNOW]) == 1


def test_snowflake_is_never_used_unless_configured(monkeypatch: pytest.MonkeyPatch):
    for key in ("ACCOUNT", "CONNECTION_NAME"):
        monkeypatch.delenv(f"PROBES_SNOWFLAKE_{key}", raising=False)
    assert SnowflakeConfig.from_env() is None
    with pytest.raises(TargetError, match="isn't configured"), open_target(SNOWFLAKE, Names.new("DB")):
        pass


def test_snowflake_config_keeps_secrets_out_of_its_description():
    config = SnowflakeConfig.from_env(
        {
            "PROBES_SNOWFLAKE_ACCOUNT": "secret-account",
            "PROBES_SNOWFLAKE_USER": "probe",
            "PROBES_SNOWFLAKE_PASSWORD": "hunter2",
            "PROBES_SNOWFLAKE_PRIVATE_KEY_FILE": "/keys/k.p8",
            "PROBES_SNOWFLAKE_PRIVATE_KEY_FILE_PWD": "passphrase",
            "PROBES_SNOWFLAKE_DATABASE": "DB",
        }
    )
    assert config
    assert config.params["authenticator"] == "SNOWFLAKE_JWT"
    for text in (config.describe(), repr(config)):
        assert not any(secret in text for secret in ("secret-account", "hunter2", "passphrase", "/keys/k.p8"))


def test_snowflake_config_needs_a_database():
    with pytest.raises(TargetError, match="PROBES_SNOWFLAKE_DATABASE"):
        SnowflakeConfig.from_env({"PROBES_SNOWFLAKE_ACCOUNT": "a"})
